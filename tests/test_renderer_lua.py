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
import re
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
    def _run(self, script, env=None):
        return subprocess.run(
            [LUA, os.path.join(LUA_DIR, script), RENDERER],
            cwd=LUA_DIR, capture_output=True, text=True, timeout=120,
            env=env)

    def _assert_suite_passed(self, proc):
        if proc.returncode != 0:
            self.fail("%s\n%s" % (proc.stdout, proc.stderr))
        # A silent pass would also be a pass if the script exited early
        # before running anything, so check it reported a plan.
        self.assertIn("1..", proc.stdout, "no test plan in the output")
        self.assertNotIn("not ok", proc.stdout)

    @staticmethod
    def _session_env(**overrides):
        """A pinned session for the clipboard tests. The renderer picks its
        clipboard helper off WAYLAND_DISPLAY / DISPLAY, so an unpinned
        environment silently tests whichever session the developer happens
        to be sitting in — and never the other one."""
        env = dict(os.environ)
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("DISPLAY", None)
        env.update(overrides)
        return env

    def test_the_renderer_suite_passes_on_x11(self):
        self._assert_suite_passed(
            self._run("test_renderer.lua",
                      env=self._session_env(DISPLAY=":0")))

    def test_the_renderer_suite_passes_on_wayland(self):
        """Same suite, Wayland session: the clipboard block asserts wl-copy
        rather than xclip. A Wayland session usually also answers xclip
        through XWayland, which is a *different* clipboard, so picking the
        wrong one would look like copy working and paste returning stale
        text."""
        self._assert_suite_passed(
            self._run("test_renderer.lua",
                      env=self._session_env(WAYLAND_DISPLAY="wayland-0")))

    def test_the_renderer_parses_under_this_interpreter(self):
        """Cheap syntax gate, separate from the behavioural run: a parse
        error otherwise surfaces as a wall of failed assertions."""
        proc = subprocess.run(
            [LUA, "-e", "assert(loadfile(%r))" % RENDERER],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(
            proc.returncode, 0,
            proc.stderr + "\n\n" + self.LOCAL_CEILING_HELP
            if "local variables" in proc.stderr else proc.stderr)

    #: Shown when the parse fails on Lua's per-function local limit, because
    #: the raw message ("main function has more than 200 local variables")
    #: names neither the cause nor the fix, and points at the last line of a
    #: 4,500-line file rather than at whatever was just added.
    LOCAL_CEILING_HELP = (
        "renderer.lua's main chunk is at Lua's 200-local ceiling, so the\n"
        "file-scope `local` that was just added is one too many. It is not\n"
        "the line the error points at — that is simply where the compiler\n"
        "gave up.\n\n"
        "Options, cheapest first:\n"
        "  * put single-use helpers inside their one caller;\n"
        "  * hang new tunables off the `state` table (rcost and render_duty do);\n"
        "  * group a family of related constants into one table local.\n\n"
        "tests/test_renderer_lua.py::test_there_is_a_local_budget_left\n"
        "reports how much room is left before this happens again.")

    #: Locals to probe for when measuring headroom. Above this we stop
    #: caring — anything that far from the ceiling is not a hazard.
    HEADROOM_PROBE = 24

    def _headroom(self):
        """How many more file-scope locals renderer.lua could take."""
        with open(RENDERER, encoding="utf-8") as fh:
            source = fh.read()
        # After the first line, so the shebang-less header comment stays put
        # and the padding is unambiguously file scope.
        head, _nl, rest = source.partition("\n")
        for n in range(self.HEADROOM_PROBE + 1):
            pad = "\n".join("local __pad%d = %d" % (i, i) for i in range(n))
            probe = "%s\n%s\n%s" % (head, pad, rest)
            check = subprocess.run(
                [LUA, "-e",
                 "local f, e = load(io.read('*a')); if not f then "
                 "io.stderr:write(e or 'err'); os.exit(1) end"],
                input=probe, capture_output=True, text=True, timeout=60)
            if check.returncode != 0:
                return n - 1
        return self.HEADROOM_PROBE

    def test_no_renderer_helper_is_defined_and_never_called(self):
        """A `function state.X` nobody calls is a feature that is wired up
        everywhere except where it fires.

        Written after exactly that: the comic reader's wheel handler was
        defined, the message that configures it arrived, the drag and the
        ctrl+wheel paths that read the same state both worked — and the
        branch that *calls* it never made it into `on_wheel`, so plain
        scrolling did nothing while everything around it behaved. Nothing
        failed, in Lua or in Python; the function was simply unreachable.

        Scoped to the `state.`/`keyclaim.` helpers because those are the
        ones that exist to
        dodge the local ceiling: a file-scope `local function` that nobody
        calls is at least visible as an unused name, while a table field
        assigned in one place and read in none looks exactly like a field
        that is used somewhere else in a 4,500-line file.
        """
        with open(RENDERER, encoding="utf-8") as fh:
            source = fh.read()
        defined = set(re.findall(
            r"^function (?:state|keyclaim)\.(\w+)\s*\(", source, re.M))
        self.assertTrue(defined, "no state helpers found — has the "
                                 "convention changed?")
        dead = []
        for name in sorted(defined):
            # Every mention that is not the definition line.
            uses = re.findall(r"(?:state|keyclaim)\.%s\b" % name,
                              source)
            if len(uses) < 2:
                dead.append(name)
        self.assertEqual(
            dead, [],
            "defined on `state` but never called:\n  "
            + "\n  ".join(dead)
            + "\n\nEither call it, or delete it. A helper that is only "
              "ever defined is a branch that silently does nothing.")

    def test_there_is_a_local_budget_left(self):
        """The ceiling is not a wall you should discover by hitting it.

        Lua allows 200 locals per function, and renderer.lua's main chunk is
        one function — every top-level `local`, constant or helper, spends
        from the same budget. It has been hit more than once, and the
        failure is opaque: a load error naming a line at the end of the file
        that has nothing to do with the change.

        This reports the remaining room, so running out is a decision rather
        than a surprise.
        """
        headroom = self._headroom()
        self.assertGreaterEqual(
            headroom, 0,
            "renderer.lua does not compile.\n\n" + self.LOCAL_CEILING_HELP)
        if headroom == 0:
            self.skipTest(
                "renderer.lua is AT the 200-local ceiling: the next "
                "file-scope local will break it.\n" + self.LOCAL_CEILING_HELP)


if __name__ == "__main__":
    unittest.main()
