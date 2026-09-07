"""`self._video` answers two questions; a site has to say which one.

The rule is in `tools/audit_video_predicate.py`. This is the guard, and it
exists because the rule survived its own repair: an audit run for exactly
this shape fixed two sites and missed a third one call below the method it
fixed, plus two more elsewhere. Prose plus a careful reading has now failed
at it twice, which is the case for a checker.

Adding a `self._video` truth-test is not a defect -- most of them are the
right question. It just has to be declared, and writing the declaration is
where a reader finds out `_library_showing()` exists.
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
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import audit_video_predicate as audit      # noqa: E402


class RawVideoPredicateTest(unittest.TestCase):

    def test_every_site_is_declared(self):
        bad = audit.undeclared()
        self.assertEqual(
            [], bad,
            "a new `self._video` truth-test is undeclared. Decide which "
            "question it asks -- \"is something playing\" (audio counts) or "
            "\"is the library on screen\" (`_library_showing()`, because "
            "music keeps `_video` set AND keeps the browser up) -- then add "
            "it to DECLARED in tools/audit_video_predicate.py with the "
            "reason. Sites: %r" % (bad,))

    def test_no_declaration_has_gone_stale(self):
        """A declaration for a site that no longer exists is a claim about
        code that is not there, and the next reader believes it."""
        reached = {(base, fn) for base, fn, _ in audit.find()}
        stale = sorted(set(audit.DECLARED) - reached)
        self.assertEqual(
            [], stale,
            "these declarations name functions that no longer test "
            "self._video; delete them: %r" % (stale,))

    def test_the_declared_files_still_exist(self):
        """FILES is a path list, and a renamed module would empty this
        checker silently -- passing while covering nothing."""
        root = audit.ROOT
        missing = [f for f in audit.FILES
                   if not os.path.exists(os.path.join(root, f))]
        self.assertEqual([], missing)
        self.assertTrue(audit.find(), "the audit found no sites at all")


class TheFinderHasTeethTest(unittest.TestCase):
    """The negative control, on synthetic source rather than by breaking the
    tree: a checker that reports nothing passes just as quietly whether it
    works or is inert."""

    def _hits(self, src):
        finder = audit._Finder()
        finder.visit(ast.parse(src))
        return sorted({fn for fn, _ in finder.hits})

    def test_it_sees_each_spelling(self):
        for src in (
            "def f(self):\n    if self._video is None: pass",
            "def f(self):\n    if self._video is not None: pass",
            "def f(self):\n    if not self._video: pass",
            "def f(self):\n    if self._video: pass",
            "def f(self):\n    if self._video and x: pass",
            "def f(self):\n    if a or not self._video: pass",
            "def f(self):\n    return bool(self._video and x)",
            "def f(self):\n    return self._video is not None",
            "def f(self):\n    while self._video: pass",
            "def f(self):\n    x = 1 if self._video else 2",
        ):
            with self.subTest(src=src.splitlines()[-1].strip()):
                self.assertEqual(["f"], self._hits(src))

    def test_it_ignores_uses_that_are_not_questions(self):
        """Reading through `_video` or assigning it is not asking whether
        the library is on screen, and reporting those would make the
        declaration table meaningless."""
        for src in (
            "def f(self):\n    self._video = x",
            "def f(self):\n    return self._video.parent.has_next",
            "def f(self):\n    self._video.aid = 2",
            "def f(self):\n    return self._video.item",
            "def f(self):\n    if other._video is None: pass",
        ):
            with self.subTest(src=src.splitlines()[-1].strip()):
                self.assertEqual([], self._hits(src))

    def test_an_undeclared_site_is_reported(self):
        """End to end, against a temporary tree -- the assertion the guard
        above makes only if this machinery is live."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "jellyfin_mpv_shim")
            os.makedirs(pkg)
            with open(os.path.join(pkg, "player.py"), "w",
                      encoding="utf-8") as fh:
                fh.write("class P:\n"
                         "    def brand_new_thing(self):\n"
                         "        if self._video is None:\n"
                         "            return 1\n")
            found = audit.undeclared(root=tmp)
        self.assertEqual([("player.py", "brand_new_thing", 3)], found)


if __name__ == "__main__":
    unittest.main()
