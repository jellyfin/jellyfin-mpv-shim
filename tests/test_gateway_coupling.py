"""The gateway's cross-domain coupling has a fixed, declared length.

`gateway/base.py` declares under ``TYPE_CHECKING`` the calls a gateway domain
makes into a *sibling* domain. The point of declaring them is that the list is
short and visible: if it grows, the twelve-domain split is drifting back toward
the 102-method facade it replaced, and the next person can see it happening.

Prose cannot hold that. The module docstring claimed **four** from the day of
the split and there were only ever three -- and a guard whose count is already
wrong is one nobody can act on. So the count is an assertion instead.

Adding a genuine fourth crossing is allowed; it is a decision, not an accident,
so update EXPECTED here in the same commit and say why in the message.
"""

import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(ROOT, "jellyfin_mpv_shim", "mpvtk_browser", "gateway",
                    "base.py")

#: Every call a gateway domain makes into a sibling domain, provided by the
#: composed ``PlayerGateway`` rather than by the mixin that calls it.
EXPECTED = {"play_list", "rebuild_source", "offline_source"}


def _declared():
    with open(BASE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.If)
                and isinstance(node.test, ast.Name)
                and node.test.id == "TYPE_CHECKING"):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(child.name)
    return names


class GatewayCouplingTest(unittest.TestCase):
    def test_the_cross_domain_calls_are_the_declared_ones(self):
        self.assertEqual(
            _declared(), EXPECTED,
            "the gateway's cross-domain coupling changed. Adding one is a "
            "decision -- update EXPECTED and say why. Removing one is good "
            "news and wants the same edit.")

    def test_the_docstring_agrees_with_the_declarations(self):
        """A count in prose beside a list is exactly what goes stale."""
        with open(BASE, encoding="utf-8") as fh:
            doc = ast.get_docstring(ast.parse(fh.read())) or ""
        words = {3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}
        right = words[len(EXPECTED)]
        self.assertIn(
            right, doc,
            "base.py's docstring should say %r, matching the %d declared "
            "cross-domain calls." % (right, len(EXPECTED)))
        for n, word in words.items():
            if n != len(EXPECTED):
                self.assertNotIn(
                    "the %s calls" % word, doc,
                    "base.py's docstring still says %r." % word)
