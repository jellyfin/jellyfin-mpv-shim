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
