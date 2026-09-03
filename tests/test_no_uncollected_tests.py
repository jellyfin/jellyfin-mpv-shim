"""Every test defined in `tests/` is actually collected and actually run.

A test that cannot run is worse than no test: it is a green tick against a
rule nobody is enforcing, and it reads as coverage in every review after it.
`tests/test_no_fake_gaps.py` catches the fake that makes a path unreachable;
this catches the case one level up, where the test itself is unreachable.

The one that prompted this was `test_player_auth_scope`: four members --
including the two asserting that our own subtitle sidecar keeps a credential
whenever mpv is not carrying the auth header -- had drifted **inside**
`if __name__ == "__main__":`, positioned after the `unittest.main()` call.
Five of that module's seven tests were collected. The other two could not run
under `discover` (wrong scope) and could not run as a script either
(`unittest.main()` exits before reaching them), and the rule they described
had gone unenforced for as long as it had existed -- which is how
`reauthorize_sidecars` came to be wired to one of the several paths that end
with the header off.

Three shapes are checked, because they are the three ways this happens:

1. a `test_*` function or `Test*` class nested inside the `__main__` guard;
2. anything at all after `unittest.main()` in that guard, which is dead
   whatever it is -- `main()` raises SystemExit;
3. a `test_*` method on a class that no test loader will collect, i.e. one
   that does not reach `unittest.TestCase`.

4. a duplicate `test_*` method name in one class, or a duplicate test class
   name in one module -- the later definition silently replaces the earlier,
   which is how this happens by copy-paste and is the shape the three above
   do not see.

Not checked: a `test_*` method inside another method, or one that a decorator
skips. Those are deliberate often enough that a guard would cost more than it
caught, and `--collect`-style counting would not tell them apart either.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import ast
import collections
import os
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _test_modules():
    for dirpath, dirnames, filenames in os.walk(TESTS_DIR):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.startswith("test_") and name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _rel(path):
    return os.path.relpath(path, os.path.dirname(TESTS_DIR))


def _is_main_guard(node):
    """``if __name__ == "__main__":`` -- in any of its spellings."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.comparators) != 1:
        return False
    left, right = test.left, test.comparators[0]
    if isinstance(right, ast.Name):     # "__main__" == __name__
        left, right = right, left
    return (isinstance(left, ast.Name) and left.id == "__name__"
            and isinstance(right, ast.Constant) and right.value == "__main__")


def _looks_like_a_test(node):
    if isinstance(node, ast.ClassDef):
        return node.name.startswith("Test") or node.name.endswith("Test") \
            or node.name.endswith("Tests")
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name.startswith("test_")
    return False


def _calls_unittest_main(node):
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    if isinstance(func, ast.Attribute):
        return func.attr == "main"
    return isinstance(func, ast.Name) and func.id == "main"


class NoUncollectedTestsTest(unittest.TestCase):
    def test_no_test_is_defined_inside_the_main_guard(self):
        found = []
        for path in _test_modules():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                if not _is_main_guard(node):
                    continue
                for child in ast.walk(node):
                    if child is node or not _looks_like_a_test(child):
                        continue
                    found.append("%s:%d %s" % (_rel(path), child.lineno,
                                               child.name))
        self.assertEqual(sorted(found), [], "\n".join(
            ["tests defined inside `if __name__ == \"__main__\":`, where no "
             "loader will find them. Move them out to module scope."] + found))

    def test_nothing_follows_unittest_main(self):
        """`unittest.main()` raises SystemExit, so anything after it in the
        same block is dead even when the file IS run as a script -- which is
        the half that makes this look like it works."""
        found = []
        for path in _test_modules():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.If, ast.Module,
                                         ast.FunctionDef)):
                    continue
                body = list(getattr(node, "body", []))
                for i, stmt in enumerate(body[:-1]):
                    if _calls_unittest_main(stmt):
                        found.append("%s:%d (%d statement(s) after it)"
                                     % (_rel(path), body[i + 1].lineno,
                                        len(body) - i - 1))
        self.assertEqual(sorted(found), [], "\n".join(
            ["code after `unittest.main()`, which never runs:"] + found))

    def test_every_test_method_is_on_a_collectable_class(self):
        """A `test_*` method on a class that never reaches
        `unittest.TestCase` is collected by nothing.

        Resolved within one module: a base class imported from elsewhere is
        taken on trust rather than followed, because the alternative is
        importing every test module to ask, and that is what this suite is
        trying not to depend on. A locally-defined base that is itself not a
        TestCase is the case that actually happens.
        """
        found = []
        for path in _test_modules():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            local, imported = {}, set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    local[node.name] = node
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        imported.add(alias.asname or alias.name.split(".")[0])

            def root_of(node):
                """The leftmost name of ``a.b.C`` -- the imported module."""
                while isinstance(node, ast.Attribute):
                    node = node.value
                return getattr(node, "id", None)

            def is_case(cls, seen=()):
                for base in cls.bases:
                    if isinstance(base, ast.Attribute):
                        name, root = base.attr, root_of(base)
                    else:
                        name, root = getattr(base, "id", None), None
                    if name in ("TestCase", "IsolatedAsyncioTestCase"):
                        return True
                    # `h.TmpDirTest` / `_e2e.E2ETestCase`: the name bound by
                    # the import is the MODULE, not the attribute, so both
                    # halves have to be offered to the trust list.
                    if name in imported or root in imported:
                        return True     # taken on trust; see the docstring
                    if name in local and name not in seen:
                        if is_case(local[name], seen + (name,)):
                            return True
                return False

            # A mixin carrying the test methods, combined into a real
            # TestCase elsewhere in the module -- `class X(_Matrix,
            # unittest.TestCase)`. Its methods run, once per combination, so
            # it is not a finding. Names only: a base list is the whole of
            # what makes this legal and it is written right there.
            mixed_in = set()
            for cls in local.values():
                if not is_case(cls):
                    continue
                for base in cls.bases:
                    name = (base.attr if isinstance(base, ast.Attribute)
                            else getattr(base, "id", None))
                    if name in local:
                        mixed_in.add(name)

            for cls in local.values():
                if is_case(cls) or cls.name in mixed_in:
                    continue
                for child in cls.body:
                    if isinstance(child, (ast.FunctionDef,
                                          ast.AsyncFunctionDef)) \
                            and child.name.startswith("test_"):
                        found.append("%s:%d %s.%s" % (_rel(path), child.lineno,
                                                      cls.name, child.name))
        self.assertEqual(sorted(found), [], "\n".join(
            ["`test_*` methods on classes that are not TestCases, so nothing "
             "collects them:"] + found))


    def test_no_test_is_shadowed_by_a_duplicate_name(self):
        """The later definition wins and the earlier one is simply gone.

        Nothing in Python or unittest complains, the module still imports,
        and the count of collected tests silently drops by one -- so a
        rebased test, or a copy-pasted case somebody forgot to rename, takes
        its predecessor with it.
        """
        found = []
        for path in _test_modules():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)

            def report(node, what):
                found.append("%s:%d %s" % (_rel(path), node.lineno, what))

            classes = collections.Counter()
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    classes[node.name] += 1
                    if classes[node.name] > 1:
                        report(node, "class %s (redefined)" % node.name)
                    methods = collections.Counter()
                    for child in node.body:
                        if not isinstance(child, (ast.FunctionDef,
                                                  ast.AsyncFunctionDef)):
                            continue
                        if not child.name.startswith("test_"):
                            continue
                        methods[child.name] += 1
                        if methods[child.name] > 1:
                            report(child, "%s.%s (redefined)"
                                   % (node.name, child.name))
        self.assertEqual(sorted(found), [], "\n".join(
            ["test names shadowed by a later definition, so the first one is "
             "never collected:"] + found))


if __name__ == "__main__":
    unittest.main()
