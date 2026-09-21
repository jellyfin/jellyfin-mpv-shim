"""The fake-mpv harness must not poison the process for later tests.

``import_player_with_fake_mpv`` installs a fake backend into ``sys.modules``
so player.py's import-time singleton constructs without a real window. That
entry is process-wide and permanent, so leaving it there handed the fake to
every *later* importer too — and ``test_mpvtk_browser`` / ``test_mpvtk_hud``
do ``import mpv as libmpv`` to spawn a real handle.

The symptom was 17 real-mpv tests failing with "renderer never became ready",
15 seconds of timeout each, only when the suite ran as a whole; every module
passed in isolation. That reads exactly like resource contention, and was
recorded as such in the migration write-up for a while (deleted in
c37bfc3e). It was module poisoning.

These run in subprocesses: checking the contract in-process would be the very
thing the contract forbids.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
# ...and the repo root. Run as a script -- which the __main__ block at the
# bottom invites -- `sys.path[0]` is this directory and the root is on the
# path nowhere, so `jellyfin_mpv_shim` resolves to whatever is pip-installed:
# silently, and it *runs*, against the previous release. Measured once as a
# renderer.lua from a fortnight ago failing a test about this tree.
# run_integration.py is unaffected (it spawns -m unittest with cwd=root).
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
import _harness as h  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

PROBE = r'''
import sys
sys.path.insert(0, %(here)r)
sys.path.insert(0, %(root)r)
import _harness as h

name = "python_mpv_jsonipc" if h.BACKEND == "jsonipc" else "mpv"
before = sys.modules.get(name)
player = h.import_player_with_fake_mpv()
after = sys.modules.get(name)

print("PLAYER_BOUND_TO_FAKE", h.is_fake_mpv(getattr(player, "mpv", None)))
print("SYSMODULES_RESTORED", after is before is not None)
fresh = __import__(name)
print("FRESH_IMPORT_IS_REAL", not h.is_fake_mpv(fresh))
'''


def _probe(backend):
    env = dict(os.environ, JMS_TEST_BACKEND=backend)
    out = subprocess.run(
        [sys.executable, "-c", PROBE % {"here": HERE, "root": ROOT}],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return dict(line.split() for line in out.stdout.split("\n") if line.strip()
                and line.split()[0].isupper())


class TestFakeMpvIsNotLeaked(unittest.TestCase):
    def _check(self, backend):
        got = _probe(backend)
        self.assertEqual(
            got.get("PLAYER_BOUND_TO_FAKE"), "True",
            "player.py must still hold the fake — the state-machine tests "
            "depend on it")
        self.assertEqual(
            got.get("SYSMODULES_RESTORED"), "True",
            "the fake was left in sys.modules; every later `import mpv` gets "
            "it, and the real-mpv tests time out waiting for a renderer")
        self.assertEqual(
            got.get("FRESH_IMPORT_IS_REAL"), "True",
            "a fresh `import mpv` returned the fake")

    @unittest.skipUnless(h.HAVE_MPV_LIB, "libmpv not available")
    def test_libmpv_backend_restores_the_real_module(self):
        self._check("libmpv")

    @unittest.skipUnless(h.HAVE_MPV_JSONIPC, "python-mpv-jsonipc not available")
    def test_jsonipc_backend_restores_the_real_module(self):
        self._check("jsonipc")


class BuildPlayerTracksTheConstructor(unittest.TestCase):
    """``build_player`` bypasses ``__init__``, so it has to re-declare every
    attribute the constructor sets.

    When it falls behind, the failure is not a clear "harness is stale" — it
    is an ``AttributeError`` raised from deep inside whatever production
    method happens to read the new attribute first, in every test that calls
    it. ``_trickplay_pending`` did exactly that: one line added to
    ``__init__`` broke 19 tests across two modules, and the traceback pointed
    at ``player.py``, which was innocent.

    Deliberate omissions go in ``ALLOWED_MISSING`` with a reason, so skipping
    one stays a decision rather than an oversight.
    """

    # Set up by the harness in a form the fake backend needs, or genuinely
    # meaningless without the real constructor's collaborators.
    ALLOWED_MISSING = {
        "_player",        # the FakeMPV, wired directly
        "_video",         # the test's video, passed in
        "menu",           # _FakeMenu
        "syncplay",       # _FakeSyncplay
        "update_check",   # _FakeUpdateCheck
        "osc_bridge",     # built against the fake pm
    }

    def _init_attrs(self):
        import ast
        path = os.path.join(ROOT, "jellyfin_mpv_shim", "player.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        cls = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "PlayerManager")
        init = next(n for n in cls.body
                    if isinstance(n, ast.FunctionDef) and n.name == "__init__")
        attrs = set()
        for node in ast.walk(init):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for t in targets:
                if (isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and t.value.id == "self"):
                    attrs.add(t.attr)
        self.assertTrue(attrs, "could not parse PlayerManager.__init__")
        return attrs

    def test_every_constructor_attribute_exists_on_a_built_player(self):
        pm = h.build_player(h.import_player_with_fake_mpv(), test=self)
        missing = sorted(a for a in self._init_attrs()
                         if a not in self.ALLOWED_MISSING
                         and not hasattr(pm, a))
        self.assertEqual(
            missing, [],
            "PlayerManager.__init__ sets these but build_player does not, so "
            "any tested method reading one raises AttributeError: %s\n"
            "Add them to build_player (or to ALLOWED_MISSING with a reason)."
            % ", ".join(missing))


class TheRefusedWriteCleanupTest(unittest.TestCase):
    """The machinery rather than the subclass: does a case that has never
    heard of this get covered by it?

    The inner cases below opt into nothing -- they are ordinary
    `unittest.TestCase`s that build a player and write a property, and
    `build_player` is what registers the cleanup. Running them and reading
    the result is the only way to assert that a *cleanup* fails a test;
    asserting the writes were recorded would test the guard again and say
    nothing about whether anything is watching.

    `property_is_absent` is what makes this non-vacuous. FakeMPV accepts every
    write, so without it the hook is installed and cannot fire, and a hook
    that cannot fire is the "tests that cannot fail" shape.
    """

    def _result(self, body):
        class Inner(unittest.TestCase):
            def runTest(inner):
                body(inner)

        result = unittest.TestResult()
        Inner().run(result)
        return result

    def test_a_write_this_mpv_refuses_fails_the_case_that_made_it(self):
        def body(inner):
            pm = h.build_player(h.import_player_with_fake_mpv(), test=inner)
            pm._player.property_is_absent("osd-shadow-offset")
            # Refused, and deliberately not raised: the write returns and the
            # test body carries on, exactly as production does.
            pm._player.osd_shadow_offset = 2

        result = self._result(body)

        self.assertEqual([], [e[1] for e in result.errors],
                         "the write raised instead of being recorded")
        self.assertEqual(1, len(result.failures),
                         "a refused property write did not fail its case")
        self.assertIn("osd_shadow_offset", result.failures[0][1],
                      "the failure does not name what was refused")

    def test_an_allowed_absence_excuses_it(self):
        """The escape hatch, on the one entry that is in the list: an mpv
        that predates `osd-border-style` is a supported mpv, and
        `set_osd_settings` writing it there is a decision."""
        absent = "osd_border_style"
        self.assertIn(absent, h.ALLOWED_REFUSED_WRITES)

        def body(inner):
            pm = h.build_player(h.import_player_with_fake_mpv(), test=inner)
            pm._player.property_is_absent(absent)
            setattr(pm._player, absent, "outline-and-shadow")

        result = self._result(body)

        self.assertEqual([], result.failures + result.errors,
                         "an absence in ALLOWED_REFUSED_WRITES failed anyway")

    def test_a_refusal_before_the_player_was_swapped_still_reports(self):
        """The record lives on the guarded CLASS, and the counter is per
        class. A case that ends holding a player of a DIFFERENT guarded class
        -- which the whole-suite leg produces, because it evicts modules
        between files and the next fake is built from a fresh base -- used to
        have its window asked of the new class, whose counter starts at zero.
        That answered "nothing was refused" for a window in which something
        was, silently. Three integration cases were doing it.
        """
        from jellyfin_mpv_shim import mpv_guard

        def body(inner):
            pm = h.build_player(h.import_player_with_fake_mpv(), test=inner)
            pm._player.property_is_absent("osd-shadow-offset")
            pm._player.osd_shadow_offset = 2      # refused, on THIS class
            # A subclass of the same fake: `guarded` caches per BASE, so a
            # new base is a new guarded class with its own counter -- which
            # is what a re-imported backend module produces, without needing
            # a second import here.
            fake_base = type(pm._player).__mro__[1]
            replacement = mpv_guard.guarded(
                type("_ReimportedFake", (fake_base,), {}))()
            self.assertIsNot(type(replacement), type(pm._player),
                             "the replacement shares the guarded class, so "
                             "this case no longer constructs the situation")
            self.assertEqual(0, mpv_guard.refused_count(replacement),
                             "the replacement's counter is not fresh")
            pm._player = replacement

        result = self._result(body)

        self.assertEqual([], [e[1] for e in result.errors],
                         "the cleanup raised instead of reporting")
        self.assertEqual(1, len(result.failures),
                         "a refusal made before the player was replaced was "
                         "dropped")
        self.assertIn("osd_shadow_offset", result.failures[0][1])

    def test_a_case_that_refuses_nothing_passes(self):
        """The control: the cleanup is not failing everything."""
        def body(inner):
            pm = h.build_player(h.import_player_with_fake_mpv(), test=inner)
            pm._player.osd_shadow_offset = 2

        result = self._result(body)

        self.assertEqual([], result.failures + result.errors)


if __name__ == "__main__":
    unittest.main()
