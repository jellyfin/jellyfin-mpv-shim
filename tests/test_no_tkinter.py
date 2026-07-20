"""Nothing in the package may depend on Tkinter.

The Tk library browser is gone. This is not just tidiness: `import tkinter`
succeeds on many systems and fails on others (it is a separate OS package on
most Linux distros, and absent from some Python builds), so a stray import
turns into a crash that only some users see — which is exactly how it
behaved when the Tk UI was optional and probed.

Checked by reading the source rather than by importing, so a module that is
only imported lazily inside a function is covered too — that is where the
last one lived (`mpv_shim.main` imported tkinter inside a try block).
"""

import ast
import os
import sys
import unittest

sys.argv = [sys.argv[0]]

import jellyfin_mpv_shim  # noqa: E402

PKG = os.path.dirname(os.path.abspath(jellyfin_mpv_shim.__file__))
ROOT = os.path.dirname(PKG)

BANNED = {"tkinter", "Tkinter", "ttk"}


def python_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ("__pycache__", ".git", "default_shader_pack")]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def imported_modules(path):
    """Every module name imported anywhere in the file, including inside
    functions — a lazy import is still a dependency."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


class TestTheApplicationHasNoTkinter(unittest.TestCase):
    def test_no_module_imports_tkinter(self):
        offenders = []
        for path in python_files(PKG):
            hit = imported_modules(path) & BANNED
            if hit:
                offenders.append("%s: %s"
                                 % (os.path.relpath(path, ROOT), sorted(hit)))
        self.assertEqual(offenders, [], "tkinter is back: %s" % offenders)

    def test_the_tk_browser_modules_are_gone(self):
        for name in ("gui_mgr.py", "library_browser"):
            self.assertFalse(os.path.exists(os.path.join(PKG, name)),
                             "%s still exists" % name)

    def test_the_tests_do_not_import_it_either(self):
        """A test that needs Tk is a test that cannot run in CI."""
        offenders = []
        for path in python_files(os.path.join(ROOT, "tests")):
            if os.path.basename(path) == os.path.basename(__file__):
                continue
            hit = imported_modules(path) & BANNED
            if hit:
                offenders.append("%s: %s"
                                 % (os.path.relpath(path, ROOT), sorted(hit)))
        self.assertEqual(offenders, [], "tkinter is back: %s" % offenders)

    def test_the_scan_actually_reads_files(self):
        """A walk that matched nothing would make all of the above vacuous."""
        count = sum(1 for _ in python_files(PKG))
        self.assertGreater(count, 25, "only found %d modules" % count)

    def test_the_scan_sees_lazy_imports(self):
        """The last tkinter import was inside a function, not at module
        scope — prove the checker would catch that shape."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write("def f():\n    import tkinter\n    return tkinter\n")
            path = fh.name
        self.addCleanup(os.unlink, path)
        self.assertIn("tkinter", imported_modules(path))


class TestTheUiSelectionSettingIsGone(unittest.TestCase):
    """browser_ui chose between the two browsers. With one browser it is
    meaningless, and leaving it would let someone select a UI that no longer
    exists and get no GUI at all."""

    def test_the_setting_is_not_in_the_schema(self):
        from jellyfin_mpv_shim.conf import Settings
        self.assertNotIn("browser_ui", Settings.__annotations__)

    def test_it_is_not_offered_in_the_settings_screen(self):
        from jellyfin_mpv_shim.mpvtk_browser import config

        for _section, keys in config.sections():
            self.assertNotIn("browser_ui", keys)
        self.assertNotIn("browser_ui", config.ENUMS)
        self.assertNotIn("browser_ui", config.LABEL_OVERRIDES)
        self.assertNotIn("browser_ui", config.settings_schema())

    def test_a_stale_value_in_an_existing_config_is_harmless(self):
        """Upgrading must not fail on a config.json that still has it."""
        from jellyfin_mpv_shim.conf import Settings

        s = Settings().parse_obj({"browser_ui": "tk",
                                  "always_transcode": True})
        self.assertTrue(s.always_transcode,
                        "a stale key stopped the rest of the config loading")
        self.assertFalse(hasattr(s, "browser_ui"),
                         "the removed key was resurrected from config.json")


if __name__ == "__main__":
    unittest.main()
