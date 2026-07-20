"""Run the Lua-side renderer tests as part of the normal suite.

renderer.lua holds state Python cannot see — scroll offsets, textbox edits,
focus — and until now had no tests at all: two protocol additions (the
textbox `commit` event, `follow` scroll containers) were written and shipped
against nothing but hand testing. tests/lua/ loads the real renderer against
a faked mpv and drives it through the real script-message boundary.

Skipped when no Lua interpreter is installed. That makes it invisible on a
bare machine, which is the tradeoff for not adding a dependency — CI and any
developer with mpv (which embeds Lua) will have one.
"""

import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LUA_DIR = os.path.join(ROOT, "tests", "lua")
RENDERER = os.path.join(ROOT, "jellyfin_mpv_shim", "mpvtk", "renderer.lua")

# luajit first: it is what mpv itself usually embeds, so it is the dialect
# the renderer actually has to run under.
INTERPRETERS = ("luajit", "lua5.1", "lua5.2", "lua5.3", "lua5.4", "lua")


def find_lua():
    for name in INTERPRETERS:
        path = shutil.which(name)
        if path:
            return path
    return None


LUA = find_lua()


@unittest.skipIf(LUA is None,
                 "no Lua interpreter (tried: %s)" % ", ".join(INTERPRETERS))
class TestRendererLua(unittest.TestCase):
    def _run(self, script):
        return subprocess.run(
            [LUA, os.path.join(LUA_DIR, script), RENDERER],
            cwd=LUA_DIR, capture_output=True, text=True, timeout=120)

    def test_the_renderer_suite_passes(self):
        proc = self._run("test_renderer.lua")
        if proc.returncode != 0:
            self.fail("%s\n%s" % (proc.stdout, proc.stderr))
        # A silent pass would also be a pass if the script exited early
        # before running anything, so check it reported a plan.
        self.assertIn("1..", proc.stdout, "no test plan in the output")
        self.assertNotIn("not ok", proc.stdout)

    def test_the_renderer_parses_under_this_interpreter(self):
        """Cheap syntax gate, separate from the behavioural run: a parse
        error otherwise surfaces as a wall of failed assertions."""
        proc = subprocess.run(
            [LUA, "-e", "assert(loadfile(%r))" % RENDERER],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
