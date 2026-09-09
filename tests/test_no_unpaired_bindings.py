"""Every forced key binding in the renderer is released by something.

`mp.add_forced_key_binding` outranks the user's own input.conf and mpv's
defaults and stays until something names it to `mp.remove_key_binding`. A
binding nobody releases does not fail, log or degrade -- it quietly keeps
eating that key for the rest of the session, and the symptom is "the mouse
stopped working", which points nowhere near a binding.

#737 was one: the Skip button took `mbtn_left` in right-click-to-pause mode
and only the ENTER half was released, so every skip segment left another
handler behind and, once the button was down, every click ran
`begin-vo-dragging` instead of reaching the UI.

**The renderer suite could not have caught the next one.** It has 400
assertions and a fake that models the binding registry, so a test *can*
assert that one name is gone -- and one now does. What no test does is
enumerate the pairs, which is why this is a lint and not another case.

A finding here is not automatically a bug: release the binding where its
owner goes away, or add it to `tools/audit_key_bindings.ACCEPTED` with the
reason it needs no release.
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
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RENDERER = os.path.join(ROOT, "jellyfin_mpv_shim", "mpvtk", "renderer.lua")


def _audit():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "audit_key_bindings", os.path.join(ROOT, "tools",
                                           "audit_key_bindings.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class UnpairedBindingsTest(unittest.TestCase):
    def test_every_binding_is_released(self):
        mod = _audit()
        findings = mod.audit(RENDERER)
        self.assertEqual(
            [], findings,
            "forced key bindings with no release: %s"
            % "; ".join("%s (line %d)" % (n, ln) for ln, n, _k in findings))

    def test_the_lint_can_actually_fail(self):
        """Guard on the guard. A pairing check that matched everything --
        an over-broad prefix rule, say -- would pass this file for ever and
        say nothing. Feed it a binding that is plainly unreleased.
        """
        import tempfile

        mod = _audit()
        src = (
            "mp.add_forced_key_binding('mbtn_left', 'jms_probe_leak',\n"
            "    function() end)\n"
            "mp.remove_key_binding('something_else')\n")
        with tempfile.NamedTemporaryFile("w", suffix=".lua",
                                         delete=False) as fh:
            fh.write(src)
            path = fh.name
        self.addCleanup(os.unlink, path)
        findings = mod.audit(path)
        self.assertEqual(1, len(findings), findings)
        self.assertEqual("jms_probe_leak", findings[0][1])

    def test_a_released_binding_is_not_reported(self):
        """The other direction: a correct pair must not be a finding, or
        the lint is noise and gets exempted into uselessness."""
        import tempfile

        mod = _audit()
        src = (
            "mp.add_forced_key_binding('mbtn_left', 'jms_probe_ok',\n"
            "    function() end)\n"
            "mp.remove_key_binding('jms_probe_ok')\n"
            "mp.add_forced_key_binding(key, 'jms_pre_' .. key, fn)\n"
            "mp.remove_key_binding('jms_pre_' .. key)\n")
        with tempfile.NamedTemporaryFile("w", suffix=".lua",
                                         delete=False) as fh:
            fh.write(src)
            path = fh.name
        self.addCleanup(os.unlink, path)
        self.assertEqual([], mod.audit(path))

    def test_accepted_entries_name_a_mechanism(self):
        """An exemption is only worth having if it says *how* the binding
        is released -- otherwise it is a silenced finding."""
        mod = _audit()
        for name, reason in mod.ACCEPTED.items():
            self.assertGreater(
                len(reason), 40,
                "%s is exempted without explaining the release" % name)


if __name__ == "__main__":
    unittest.main()
