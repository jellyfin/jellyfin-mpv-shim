"""The browser reaches the player through `PlayerManager`, or says why not.

`_act` defers whenever the player lock is busy, and the lock is held for the
whole of a playback start -- so a `_act` body runs at an unknown time with
no lock held. `PlayerManager`'s methods are where the rules about that live.
A body that writes `pm._player.<prop>` instead skips every one of them, and
both live examples cost a user something no setting explains: R7 made every
film in a session play stretched, R8 made every film start windowed.

Runs `tools/audit_act_targets.py`. A finding here is not automatically a
bug: route it through the manager method, or add the site to `DECLARED`
with the reason it is the right shape.
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

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_act_targets as audit    # noqa: E402


class ActTargetsTest(unittest.TestCase):
    def test_nothing_reaches_the_handle_undeclared(self):
        findings = audit.audit()
        if findings:
            self.fail(
                "\n".join(
                    "%s:%d rule %s -- %s reaches `_player.%s`"
                    % (s.module, s.line, s.rule, s.func, s.prop)
                    for s in findings)
                + "\n\nSee tools/audit_act_targets.py — route it through "
                  "the PlayerManager method, or add the site to DECLARED "
                  "with the reason.")

    def test_no_declaration_outlives_its_site(self):
        """The guard on the guard. A key for a site that moved or went away
        is not harmless bookkeeping: it pre-authorises the next reach with
        the same name, and the audit stays quiet because the key is already
        there. Writing this list by hand produced six such keys on the
        first run, which is why it is a test and not a habit."""
        self.assertEqual([], audit.stale())

    def test_every_declaration_says_why(self):
        for key, why in audit.DECLARED.items():
            with self.subTest(key):
                self.assertGreater(
                    len(why), 60,
                    "%s is declared without saying what makes it the right "
                    "shape" % key)

    def test_a_write_is_found_in_both_spellings(self):
        """`x._player.p = v` and `setattr(x._player, "p", v)` are the same
        reach, and the gateway uses both. A rule that saw one of them would
        pass this package today and miss the next line somebody writes."""
        for src, prop in (
            ("def f(pm):\n    pm._player.volume = 5\n", "volume"),
            ("def f(pm):\n    setattr(pm._player, 'volume', 5)\n", "volume"),
        ):
            with self.subTest(src.strip()):
                found = self._sites_in(src)
                writes = [s for s in found if s.rule == "A"]
                self.assertEqual(1, len(writes), found)
                self.assertEqual(prop, writes[0].prop)

    def test_a_computed_property_name_is_still_a_write(self):
        """`setattr(pm._player, name, v)` cannot be attributed to a
        property, which makes it the worst version of this reach rather
        than an exempt one."""
        found = self._sites_in(
            "def f(pm, name):\n    setattr(pm._player, name, 5)\n")
        writes = [s for s in found if s.rule == "A"]
        self.assertEqual(["<computed>"], [s.prop for s in writes])

    def test_the_handle_taken_by_getattr_is_a_read(self):
        """It is the same reach with the name as a string, so no Attribute
        node carries it. The package had one when this file was written."""
        found = self._sites_in(
            "def f(pm):\n    p = getattr(pm, '_player', None)\n    return p\n")
        self.assertEqual([("f", "B")], [(s.func, s.rule) for s in found])

    def test_a_write_is_not_also_counted_as_a_read(self):
        """One reach, one finding. Reporting both halves of `setattr` would
        make every repair look like two and every count wrong."""
        found = self._sites_in(
            "def f(pm):\n    setattr(pm._player, 'volume', 5)\n")
        self.assertEqual(["A"], [s.rule for s in found])

    def test_a_nested_def_is_named_by_its_chain(self):
        """`flip` says nothing on its own and there can be two of them in a
        file; the declaration has to name a site, not a common word."""
        found = self._sites_in(
            "def outer(self):\n"
            "    def flip(pm):\n"
            "        pm._player.fullscreen = True\n"
            "    self._act(flip)\n")
        self.assertEqual(["outer.flip"], [s.func for s in found])

    @staticmethod
    def _sites_in(src):
        """Run the module scanner over a snippet, via a temp file -- the
        scanner reads source rather than importing, which is what lets it
        run against a package whose import has side effects."""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".py",
                                         delete=False) as fh:
            fh.write(src)
            path = fh.name
        try:
            ast.parse(src)          # a broken snippet is a broken test
            return audit._scan(path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
