"""No caller may draw a parked bitmap without asking whether it still exists.

`tools/audit_keep_result.py` has the explanation; this runs it over the
package so the check is part of the suite.
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
import sys
import tempfile
import textwrap
import unittest

sys.argv = [sys.argv[0]]

import jellyfin_mpv_shim  # noqa: E402

PKG = os.path.dirname(os.path.abspath(jellyfin_mpv_shim.__file__))
TOOLS = os.path.join(os.path.dirname(PKG), "tools")


def load_audit():
    """Import the tool by path — `tools/` is not a package."""
    import importlib.util
    path = os.path.join(TOOLS, "audit_keep_result.py")
    spec = importlib.util.spec_from_file_location("audit_keep_result", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IgnoredKeepTest(unittest.TestCase):
    def setUp(self):
        self.audit = load_audit()

    def scan_source(self, source):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "page.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(source))
            return list(self.audit.scan_file(path, "page.py"))

    def test_the_package_has_none(self):
        base = os.path.dirname(PKG)
        new = []
        for dirpath, dirnames, filenames in os.walk(PKG):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", "default_shader_pack")]
            for name in sorted(filenames):
                if name.endswith(".py"):
                    path = os.path.join(dirpath, name)
                    new += [d for k, d in self.audit.scan_file(
                        path, os.path.relpath(path, base))
                        if k not in self.audit.ACCEPTED]
        self.assertEqual(new, [], "\n  ".join(
            ["keep() answers ignored (docs/browser-shell.md §6):"] + new))

    def test_a_bare_keep_is_reported(self):
        """The shape the reader shipped with, so the audit is not vacuous."""
        found = self.scan_source("""
            class Reader:
                def page(self, store, route):
                    entry = route.get("_entry")
                    store.keep(entry)
                    return entry
        """)
        self.assertEqual([k for k, _ in found], ["page.py::Reader.page"])

    def test_a_keep_whose_answer_is_used_is_not(self):
        self.assertEqual(self.scan_source("""
            def page(store, entry):
                if not store.keep(entry):
                    return None
                ok = store.keep(entry)
                return entry if ok else None
        """), [])


if __name__ == "__main__":
    unittest.main()
