"""A literal mpv key name that a setting can move has to be declared.

The recurring shape, four times over: a function resolves a `kb_*` /
`ui_select_key` / `hud_wake_key` setting on one line and writes the default
spelling as a literal on the next. R10 sits one screen from R3, inside a
function that resolves the very setting it then hardcodes, and four surveys
plus a session of hand work walked past it -- so this is a predicate rather
than a fifth careful reading.

Runs `tools/audit_frozen_key_literals.py`. A finding is not automatically a
bug: resolve the setting, or declare the site with the reason the literal is
right. What the declaration buys is that the next person to add one has to
answer the question, and finds out from it that a resolver exists.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import os
import re
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_frozen_key_literals as audit    # noqa: E402

RENDERER = os.path.join(ROOT, "jellyfin_mpv_shim", "mpvtk", "renderer.lua")


class FrozenKeyLiteralsTest(unittest.TestCase):
    def test_every_site_is_declared(self):
        found = audit.undeclared()
        if found:
            self.fail(
                "\n".join("%s:%d %r in %s"
                          % (os.path.relpath(s.path, ROOT), s.line,
                             s.literal, s.scope) for s in found)
                + "\n\nSee tools/audit_frozen_key_literals.py — resolve the "
                  "setting, or declare the site with the reason.")

    def test_no_declaration_outlives_its_site(self):
        """A key matching nothing pre-authorises the next literal written in
        a scope by that name. Writing this table by hand put six entries on
        functions that hold no key literal at all, and a seventh (R10) on
        the wrong function entirely, so the check is not theoretical."""
        self.assertEqual([], audit.stale())

    def test_every_declaration_says_why(self):
        for key, (status, why) in audit.DECLARED.items():
            with self.subTest(key):
                self.assertIn(status, (audit.OPEN, audit.OK))
                self.assertGreater(
                    len(why), 60,
                    "%s is declared without a reason a reader can act on"
                    % key)

    def test_the_open_findings_are_still_named(self):
        """These are declared as FINDINGS, not exemptions, and the whole
        value of that is a reader seeing it. If someone repairs one, the
        declaration has to stop claiming it is open — and the way that
        surfaces is this list going stale.

        The status is a field, not a word in the reason. The first draft
        read `"OPEN" in why` and disagreed with itself over two entries
        whose prose said "open with it" and "SECOND SITE" — which is the
        same defect the tool is about, in the tool.
        """
        self.assertEqual(
            ["app.py:MpvtkBrowser._shell_claimed_keys",
             "app.py:MpvtkBrowser._shell_key",
             "player.py:PlayerManager",
             "renderer.lua:PHUD_SUMMON_KEYS",
             "renderer.lua:phud_bind_summon",
             "renderer.lua:phud_bind_wake",
             "renderer.lua:phud_skip_bind"],
            audit.open_findings())

    def test_the_key_names_come_from_conf(self):
        """Listing them here instead would stop covering a new `kb_*` the
        day it is added, silently. Reading `conf.py` means a renamed setting
        changes the answer loudly."""
        keys = audit._key_names()
        self.assertEqual({"ENTER", "ESC", "SPACE", "UP", "DOWN",
                          "LEFT", "RIGHT"}, keys)

    def test_a_new_frozen_literal_is_reported(self):
        """Guard on the guard: a literal in a scope nobody declared has to
        be a finding, or the table is decoration."""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".lua",
                                         delete=False) as fh:
            fh.write("local function jms_probe()\n"
                     "    mp.add_forced_key_binding('ENTER', 'x', f)\n"
                     "end\n")
            path = fh.name
        self.addCleanup(os.unlink, path)
        found = audit._scan_lua(path, audit._key_names())
        self.assertEqual([("jms_probe", "ENTER")],
                         [(s.scope, s.literal) for s in found])

    def test_a_key_name_in_a_comment_is_not_a_site(self):
        """`renderer.lua` explains these keys at length. Charging prose
        would bury the real sites under its own documentation."""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".lua",
                                         delete=False) as fh:
            fh.write("-- we force 'ENTER' here for the reason above\n"
                     "local function jms_probe()\n"
                     "    return 1\n"
                     "end\n")
            path = fh.name
        self.addCleanup(os.unlink, path)
        self.assertEqual([], audit._scan_lua(path, audit._key_names()))

    def test_a_file_scope_constant_is_not_charged_to_the_function_above(self):
        """The scope tracker's own bug, kept as a case. `PHUD_SUMMON_KEYS`
        is R3 and it follows `phud_wake_key`; charging it there put the R3
        declaration on the resolver that is the counter-example to it."""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".lua",
                                         delete=False) as fh:
            fh.write("local function jms_resolver()\n"
                     "    return state.k or 'ESC'\n"
                     "end\n"
                     "\n"
                     "local JMS_KEYS = { 'ENTER' }\n")
            path = fh.name
        self.addCleanup(os.unlink, path)
        found = audit._scan_lua(path, audit._key_names())
        self.assertEqual(
            [("jms_resolver", "ESC"), ("JMS_KEYS", "ENTER")],
            [(s.scope, s.literal) for s in found])

    def test_the_scan_sees_every_uppercase_literal_in_the_renderer(self):
        """Scope attribution is a labelling job and must never be a filter.
        The scope tracker was rewritten once already; this is what says the
        rewrite moved sites between labels rather than dropping them."""
        keys = audit._key_names()
        with open(RENDERER, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        raw = set()
        for lineno, line in enumerate(lines, 1):
            if line.lstrip().startswith("--"):
                continue
            for match in re.finditer(r"'([^'\n]*)'|\"([^\"\n]*)\"", line):
                value = match.group(1) or match.group(2)
                if value in keys:
                    raw.add((lineno, value))
        got = {(s.line, s.literal) for s in audit.sites()
               if s.path.endswith(".lua")}
        self.assertEqual(raw, got)


if __name__ == "__main__":
    unittest.main()
