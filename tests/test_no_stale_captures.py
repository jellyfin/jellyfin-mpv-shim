"""No handler may fire with state that was read while drawing.

`tools/audit_stale_captures.py` has the explanation and the accept-list;
this runs it, so the check is part of the suite rather than something to
remember. Two reasons it is worth a test of its own:

* **The suite cannot catch this any other way.** A screen is rebuilt from
  scratch each repaint, so a handler closes over a snapshot; `build_scene`
  renders when asked and therefore draws a correct tree whether or not the
  app would ever have redrawn. The detail page's Play button passed its own
  test for months while playing the previously-selected audio track — the
  test rebuilt the scene between the pick and the click, which is exactly
  the accidental repaint that hid it in the app.
* **It is a source-level property**, like `test_no_tkinter`. Reading the
  code answers it completely and costs nothing.

A failure here is not necessarily a bug: it is a capture nobody has ruled
on. Either read the state inside the handler — which is always correct, and
what the fix looked like — or add the finding to `ACCEPTED` with the reason
it cannot go stale.
"""

import os
import sys
import unittest

sys.argv = [sys.argv[0]]

import jellyfin_mpv_shim  # noqa: E402

PKG = os.path.dirname(os.path.abspath(jellyfin_mpv_shim.__file__))
TOOLS = os.path.join(os.path.dirname(PKG), "tools")


def load_audit():
    """Import the tool by path — `tools/` is not a package."""
    import importlib.util
    path = os.path.join(TOOLS, "audit_stale_captures.py")
    spec = importlib.util.spec_from_file_location("audit_stale_captures",
                                                  path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StaleCaptureTest(unittest.TestCase):
    def setUp(self):
        self.audit = load_audit()

    def findings(self):
        base = os.path.dirname(PKG)
        out = []
        for dirpath, dirnames, filenames in os.walk(PKG):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", "default_shader_pack")]
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                out += list(self.audit.scan_file(
                    path, os.path.relpath(path, base)))
        return out

    def test_no_unaccepted_captures(self):
        new = [detail for key, detail in self.findings()
               if key not in self.audit.ACCEPTED]
        self.assertEqual(
            new, [],
            "handler(s) closing over state read at draw time:\n  %s\n\n"
            "Read the state inside the handler, or add it to ACCEPTED in "
            "tools/audit_stale_captures.py with the reason it cannot change "
            "between the draw and the press." % "\n  ".join(new))

    def test_the_accept_list_is_not_stale(self):
        """An entry that no longer matches anything is a claim about code
        that has moved on — it would go on excusing a finding that came back
        under the same name somewhere else."""
        keys = {key for key, _detail in self.findings()}
        dead = sorted(set(self.audit.ACCEPTED) - keys)
        self.assertEqual(
            dead, [],
            "ACCEPTED names findings that no longer exist:\n  %s"
            % "\n  ".join(dead))

    def test_the_checker_still_recognizes_the_shape(self):
        """A guard on the guard. This checks source, so a refactor that
        renamed a handler keyword or a widget helper would quietly leave it
        matching nothing — and a checker that finds nothing is
        indistinguishable from a clean tree.

        The sample is the detail page's bug as it actually stood: a pair
        read from a helper that reads the route, closed over by a Play
        button. It has to be reported, and the same code reading inside the
        handler has to not be.
        """
        import ast
        bad = ast.parse(
            "class P:\n"
            "    def _tracks(self):\n"
            "        return self.route.get('_aid'), self.route.get('_sid')\n"
            "    def _buttons(self, item):\n"
            "        aid, sid = self._tracks()\n"
            "        return Button('Play', on_click=lambda: play(item, aid))\n")
        good = ast.parse(
            "class P:\n"
            "    def _tracks(self):\n"
            "        return self.route.get('_aid'), self.route.get('_sid')\n"
            "    def _buttons(self, item):\n"
            "        return Button('Play',\n"
            "                      on_click=lambda: play(item, self._tracks()))\n")

        def run(tree):
            stateful = {fn.name for fn in ast.walk(tree)
                        if isinstance(fn, ast.FunctionDef)
                        and any(self.audit.reads_mutable(s, ())
                                for s in fn.body)}
            found = []
            for fn in ast.walk(tree):
                if isinstance(fn, ast.FunctionDef):
                    found += list(self.audit.audit_function(fn, stateful))
            return found

        self.assertTrue(run(bad),
                        "the checker no longer recognizes a build-time "
                        "capture; it is matching nothing, not passing")
        self.assertEqual(run(good), [],
                         "reading inside the handler is the fix, and must "
                         "not be reported")


if __name__ == "__main__":
    unittest.main()
