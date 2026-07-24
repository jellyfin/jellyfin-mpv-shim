"""Whole-tree source invariants — mechanical checks that no reviewer reliably
performs and no behavioural test can fail on.

These are deliberately cheap AST walks over the package rather than a linter
config: the repo has no linter, and each rule here exists because the defect
it catches is invisible in a diff.
"""

import ast
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "jellyfin_mpv_shim")

# Generated / vendored trees that are not ours to hold to these rules.
SKIP_DIRS = {"messages", "default_shader_pack", "__pycache__"}


def _sources():
    for root, dirs, files in os.walk(PKG):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            if name.endswith(".py"):
                path = os.path.join(root, name)
                yield os.path.relpath(path, REPO), path


def _parsed():
    for rel, path in _sources():
        with open(path, encoding="utf-8") as fh:
            yield rel, ast.parse(fh.read(), filename=path)


class TestNoOrphanedDocstrings(unittest.TestCase):
    """A bare string expression anywhere but position 0 of a body.

    This is what an inserted guard clause does to a docstring: the string
    stops being ``__doc__`` and becomes a no-op statement, so the
    documentation is silently lost while the diff looks like a pure addition.
    It happened to ``MpvtkBrowser.enter_browse`` — the headless redirect was
    added above the docstring, and nothing anywhere noticed.

    Cheap to check, impossible to spot in review, and it also catches the
    rarer case of a comment written as a string literal.
    """

    def test_no_string_statement_follows_the_first_statement(self):
        offenders = []
        for rel, tree in _parsed():
            for node in ast.walk(tree):
                body = getattr(node, "body", None)
                if not isinstance(body, list):
                    continue
                for index, stmt in enumerate(body):
                    if index == 0:
                        continue          # a real docstring
                    if (isinstance(stmt, ast.Expr)
                            and isinstance(stmt.value, ast.Constant)
                            and isinstance(stmt.value.value, str)):
                        offenders.append("%s:%d (in %s)" % (
                            rel, stmt.lineno,
                            getattr(node, "name", type(node).__name__)))
        self.assertEqual(
            offenders, [],
            "String literals used as statements — almost always a docstring "
            "orphaned by code inserted above it, which silently drops "
            "__doc__:\n  " + "\n  ".join(offenders))


class TestComponentsAreLeaves(unittest.TestCase):
    """``mpvtk_browser/components/`` is the bottom of the UI stack.

    A component takes data plus render resources plus callbacks, and returns
    a widget tree. It must not know about the app shell, the route dict, the
    data source or navigation — those are precisely the couplings that made
    the browser a 360-method object, and the only thing keeping them out is
    a rule someone has to remember.

    So the rule is a test. See docs/ARCHITECTURE_TARGET.md §1.4 for the
    distinction being enforced: a component may need ``art`` and callbacks,
    but never ``nav``, ``source`` or ``route``.
    """

    #: Sibling modules a component may never import.
    FORBIDDEN_IMPORTS = {"app", "views", "settings", "auth", "dialogs",
                         "music", "queue_edit", "cast", "ui", "repository"}

    #: Names that give away shell coupling if a component references them.
    FORBIDDEN_NAMES = {"nav_stack", "navigate", "go_back", "run_async",
                       "_route_async", "_bump_epoch", "_load_route"}

    @staticmethod
    def _component_sources():
        base = os.path.join(PKG, "mpvtk_browser", "components")
        if not os.path.isdir(base):
            return []
        out = []
        for name in sorted(os.listdir(base)):
            if name.endswith(".py"):
                path = os.path.join(base, name)
                out.append(("mpvtk_browser/components/" + name, path))
        return out

    def test_the_package_exists(self):
        self.assertTrue(
            self._component_sources(),
            "mpvtk_browser/components/ has no modules yet — this is step 1 "
            "of docs/ARCHITECTURE_TARGET.md §3 and the invariant it "
            "establishes.")

    def test_components_do_not_import_the_shell(self):
        offenders = []
        for rel, path in self._component_sources():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom):
                    # Relative sibling import: `from .app import X` has
                    # module="app"; `from ..conf import settings` is fine.
                    if node.level == 1 and node.module:
                        names.append(node.module.split(".")[0])
                elif isinstance(node, ast.Import):
                    names += [a.name.split(".")[-1] for a in node.names]
                for name in names:
                    if name in self.FORBIDDEN_IMPORTS:
                        offenders.append("%s:%d imports %s"
                                         % (rel, node.lineno, name))
        self.assertEqual(
            offenders, [],
            "Components must not depend on the shell or the data layer:\n  "
            + "\n  ".join(offenders))

    def test_components_do_not_reference_shell_state(self):
        offenders = []
        for rel, path in self._component_sources():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and node.attr in self.FORBIDDEN_NAMES):
                    offenders.append("%s:%d touches .%s"
                                     % (rel, node.lineno, node.attr))
                elif (isinstance(node, ast.Name)
                        and node.id in self.FORBIDDEN_NAMES):
                    offenders.append("%s:%d references %s"
                                     % (rel, node.lineno, node.id))
        self.assertEqual(
            offenders, [],
            "Components must not reach for navigation or the async runner; "
            "take a callback instead:\n  " + "\n  ".join(offenders))


class TestOneOwnerForSharedMachinery(unittest.TestCase):
    """Certain state must have exactly one owning module.

    ``app.py``'s docstring already asserts this for the epoch — "``_epoch``
    and ``_lock`` live *only* here" — but nothing enforced it, and a claim in
    prose is exactly the kind of thing a decomposition erodes one mixin at a
    time. Now the claim is checked.

    The counted thing is *ownership*, not use: mixins legitimately READ the
    epoch (``ep = self._epoch``) on the loop thread and hand it to
    ``run_async``. What must not spread is the machinery — the lock, the
    pool, and the code that advances the counter.
    """

    BROWSER = os.path.join(PKG, "mpvtk_browser")

    def _browser_modules(self):
        for name in sorted(os.listdir(self.BROWSER)):
            if name.endswith(".py"):
                yield name, os.path.join(self.BROWSER, name)

    def _modules_defining(self, predicate):
        found = set()
        for name, path in self._browser_modules():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            if predicate(tree, name):
                found.add(name)
        return found

    @staticmethod
    def _assigns(tree, attr):
        """Does this module ASSIGN self.<attr> anywhere?"""
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                if (isinstance(target, ast.Attribute)
                        and target.attr == attr
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"):
                    return True
        return False

    def test_the_epoch_counter_has_one_writer(self):
        writers = self._modules_defining(
            lambda tree, _n: self._assigns(tree, "_epoch"))
        self.assertLessEqual(
            writers, {"async_runner.py", "app.py"},
            "Only the async runner may advance the epoch; every other module "
            "reads it and passes it to run_async. Writers found: %s"
            % sorted(writers))

    def test_the_async_lock_has_one_owner(self):
        owners = self._modules_defining(
            lambda tree, _n: self._assigns(tree, "_lock"))
        # strips.py and thumbnails.py own their own, unrelated caches' locks.
        owners -= {"strips.py", "thumbnails.py"}
        self.assertLessEqual(
            owners, {"async_runner.py"},
            "The async lock belongs to the runner. Owners found: %s"
            % sorted(owners))


class TestNoTopLevelMutableClassState(unittest.TestCase):
    """A mutable class attribute is shared by every instance.

    The browser's mixins declare ``ROUTES`` dicts as class attributes, which
    is correct and deliberate (``_routes()`` merges them read-only). A
    mutable class attribute that is NOT one of those is almost always an
    instance field that was written in the wrong place — and because the
    browser is a singleton in production, the bug never shows up until a
    second instance exists, which is to say in the tests, which is to say
    after the refactor that introduces one.
    """

    ALLOWED = {"ROUTES", "HEADLESS_ROUTES", "MODULES"}

    @staticmethod
    def _is_settings_schema(cls):
        """conf.Settings declares its whole schema as class attributes — that
        IS the SettingsBase contract (``__annotations__`` for the type, the
        class attribute for the default), so a ``list``-typed setting has
        nowhere else to live. Exempt rather than allow-list, or every future
        list/dict setting has to be added by name."""
        return any(isinstance(b, ast.Name) and b.id.endswith("SettingsBase")
                   or isinstance(b, ast.Attribute) and b.attr.endswith("SettingsBase")
                   for b in cls.bases)

    def test_mutable_class_attributes_are_declared_intentionally(self):
        offenders = []
        for rel, tree in _parsed():
            for cls in ast.walk(tree):
                if not isinstance(cls, ast.ClassDef):
                    continue
                if self._is_settings_schema(cls):
                    continue
                for stmt in cls.body:
                    if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                        continue
                    targets = (stmt.targets if isinstance(stmt, ast.Assign)
                               else [stmt.target])
                    value = stmt.value
                    if not isinstance(value, (ast.List, ast.Dict, ast.Set)):
                        continue
                    for target in targets:
                        if not isinstance(target, ast.Name):
                            continue
                        if target.id in self.ALLOWED:
                            continue
                        # An empty literal is the classic accident; a
                        # populated one is usually a deliberate table.
                        if not (value.elts if hasattr(value, "elts")
                                else value.keys):
                            offenders.append("%s:%d %s.%s" % (
                                rel, stmt.lineno, cls.name, target.id))
        self.assertEqual(
            offenders, [],
            "Empty mutable class attributes are shared across instances; "
            "set these in __init__ instead (or add the name to ALLOWED "
            "with a reason):\n  " + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
