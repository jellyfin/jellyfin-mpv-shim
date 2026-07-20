"""Structural tests for the MpvtkBrowser mixin split.

``MpvtkBrowser`` is assembled from one mixin per feature area (see
``mpvtk_browser/app.py``). Mixins have exactly one real hazard: if two of
them define the same method name, MRO order silently picks a winner and the
other body becomes dead code — no error, and the feature it belonged to just
stops working. These tests turn that into a failure.
"""

import ast
import inspect
import os
import unittest

from jellyfin_mpv_shim.mpvtk_browser import app as app_mod
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

PKG = os.path.dirname(inspect.getfile(app_mod))
MODULES = [m for m in ["app", "dialogs", "auth", "settings", "queue_edit",
                       "music", "views", "tiles"]
           if os.path.exists(os.path.join(PKG, m + ".py"))]


def _members(module):
    """{name: lineno} for every class-body member defined in `module`.py.

    Read from source rather than ``vars()`` so that a name defined twice in
    one module (the other way to lose a method) is also visible.
    """
    with open(os.path.join(PKG, module + ".py")) as fh:
        src = fh.read()
    out = {}
    for cls in [n for n in ast.parse(src).body if isinstance(n, ast.ClassDef)]:
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[node.name] = node.lineno
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        out[t.id] = node.lineno
    return out


# ROUTES is the one name every mixin is *meant* to define: core merges them
# by walking the MRO explicitly (MpvtkBrowser._routes), so the usual "first
# in the MRO silently wins" hazard does not apply. Its own collision check —
# no route kind claimed twice — is TestRouteTable below.
SHARED = {"ROUTES"}


class TestMixinPartition(unittest.TestCase):
    def test_no_name_is_defined_by_two_mixins(self):
        """The one real mixin hazard: a silent override across modules."""
        owner = {}
        clashes = []
        for mod in MODULES:
            for name in _members(mod):
                if name in SHARED:
                    continue
                if name in owner:
                    clashes.append(f"{name}: {owner[name]} and {mod}")
                else:
                    owner[name] = mod
        self.assertEqual(clashes, [], "members defined in two modules")

    def test_every_definition_survives_on_the_class(self):
        """Nothing was orphaned in a module that app.py forgot to mix in."""
        missing = []
        for mod in MODULES:
            for name in _members(mod):
                if not hasattr(MpvtkBrowser, name):
                    missing.append(f"{mod}.{name}")
        self.assertEqual(missing, [], "defined but not on MpvtkBrowser")

    def test_mixins_do_not_import_app(self):
        """app.py imports the mixins; the reverse would be a cycle."""
        offenders = []
        for mod in MODULES:
            if mod == "app":
                continue
            with open(os.path.join(PKG, mod + ".py")) as fh:
                src = fh.read()
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.ImportFrom) and node.module == "app":
                    offenders.append(mod)
        self.assertEqual(offenders, [])

    def test_only_core_mutates_the_epoch(self):
        """Reading ``self._epoch`` at dispatch time is the correct pattern and
        happens everywhere. *Bumping* it, and holding ``_lock``, is core's job
        — a mixin that did either would be making staleness decisions the
        router owns."""
        offenders = []
        for mod in MODULES:
            if mod == "app":
                continue
            with open(os.path.join(PKG, mod + ".py")) as fh:
                src = fh.read()
            for node in ast.walk(ast.parse(src)):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, ast.AugAssign):
                    targets = [node.target]
                elif isinstance(node, ast.With):
                    targets = [i.context_expr for i in node.items]
                for t in targets:
                    if (isinstance(t, ast.Attribute)
                            and t.attr in ("_epoch", "_lock")
                            and isinstance(t.value, ast.Name)
                            and t.value.id == "self"):
                        offenders.append(
                            f"{mod}:{node.lineno} self.{t.attr}")
        self.assertEqual(offenders, [])


class TestRouteTable(unittest.TestCase):
    """Every route kind is declared once, in the mixin that draws it, with
    both halves named — the loader and the renderer used to be a 215-line
    elif chain in app.py and a dict a thousand lines away, so adding a view
    meant editing two distant points and forgetting one was silent."""

    def _declared(self):
        """[(kind, loader, renderer, module)] straight from the mixins."""
        out = []
        for base in MpvtkBrowser.__mro__:
            table = base.__dict__.get("ROUTES") or {}
            for kind, (loader, renderer) in table.items():
                out.append((kind, loader, renderer, base.__module__))
        return out

    def test_no_kind_is_claimed_by_two_mixins(self):
        seen = {}
        clashes = []
        for kind, _l, _r, mod in self._declared():
            if kind in seen:
                clashes.append(f"{kind}: {seen[kind]} and {mod}")
            seen[kind] = mod
        self.assertEqual(clashes, [])

    def test_every_named_method_exists(self):
        """A typo in the table is otherwise a route that renders a spinner
        forever, or one that never loads."""
        missing = []
        for kind, loader, renderer, mod in self._declared():
            for name in (loader, renderer):
                if name is not None and not hasattr(MpvtkBrowser, name):
                    missing.append(f"{kind} -> {name} ({mod})")
        self.assertEqual(missing, [])

    def test_every_renderer_is_declared(self):
        """A _render_<kind> nobody routes to is dead code — this codebase has
        shipped that five times (see MIGRATION.md)."""
        routed = {r for _k, _l, r, _m in self._declared()}
        orphans = []
        for mod in MODULES:
            for name in _members(mod):
                if (name.startswith("_render_") and name not in routed
                        # not a route: chrome and sub-views of one
                        and name not in ("_render_route",)):
                    orphans.append(f"{mod}.{name}")
        self.assertEqual(orphans, [], "a renderer no route reaches")

    def test_the_merged_table_matches_what_the_mixins_declare(self):
        merged = MpvtkBrowser._routes()
        self.assertEqual(len(merged), len(self._declared()))
        for kind, loader, renderer, _mod in self._declared():
            self.assertEqual(merged[kind], (loader, renderer))


if __name__ == "__main__":
    unittest.main()
