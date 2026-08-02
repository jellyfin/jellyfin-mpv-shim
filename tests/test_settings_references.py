"""Every ``settings.<name>`` in the package resolves to a declared key.

``conf.Settings`` declares its keys as class attributes with type
annotations, and ``SettingsBase`` builds the live object from those. Nothing
checks that the *readers* agree: a key that is renamed or retired leaves
``settings.old_name`` compiling perfectly and raising ``AttributeError`` the
first time that line runs — which, for a line on the minimize path or the
browse-leave path, can be a long time after the change.

That is not hypothetical. #615 orphaned ``enable_osc`` and two call sites in
``gateway/playback.py`` kept reading it; the whole unit suite stayed green
and it crashed on the first minimize.

The scan is syntactic, and covers the three spellings that actually appear:
``settings.<name>``, an aliased import (``settings as _settings``), and
``getattr(settings, "<literal>")``. The last matters more than it looks --
most of those pass a default, so a retired key there becomes a silently
wrong answer rather than the AttributeError this guard exists to catch.

Two known holes, stated rather than papered over. A non-literal
``getattr(settings, name)`` cannot be checked at all. And setting names
appear as bare STRINGS in ``mpvtk_browser/config.py``'s SECTIONS / LABELS /
NOTES / LABELED_ENUMS, where ``sections()`` filters on ``k in schema`` and a
stale entry therefore vanishes silently; nothing enforces those.

``tests/`` is scanned too, and for a reason: two integration setups went on
writing ``settings.enable_osc = False`` after #615 retired the key, so those
legs quietly stopped suppressing the OSC they meant to suppress. A dead
write in a test is worse than one in the app, because the test keeps passing.

Under ``tests/`` only ASSIGNMENTS are checked, though. Several helpers there
take a plain dict parameter named ``settings`` and call ``.items()`` on it,
and telling that apart from the singleton needs scope analysis this does not
have. A write is the case worth catching and is never ambiguous.
"""

import ast
import os
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.conf import Settings  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "jellyfin_mpv_shim")
TESTS = os.path.dirname(os.path.abspath(__file__))

#: Directories with no settings readers in them.
SKIP_DIRS = {"__pycache__", "messages", "default_shader_pack"}

#: Names a module may import ``conf.settings`` as. Checked against the
#: import statements rather than assumed, so a new alias is caught.
def _aliases(tree):
    names = {"settings"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "settings" and alias.asname:
                    names.add(alias.asname)
    return names


def _sources():
    for base in (PKG, TESTS):
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                if name.endswith(".py"):
                    yield os.path.join(root, name)


class TestEverySettingsReferenceResolves(unittest.TestCase):
    def test_they_all_exist(self):
        # Annotations are the declared keys; dir() adds the methods and
        # properties SettingsBase and Settings define (load, migrate, ...),
        # which are legitimate reads too.
        known = (set(Settings.__annotations__)
                 | {n for n in dir(Settings) if not n.startswith("__")})
        missing = {}
        for path in _sources():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), path)
            names = _aliases(tree)
            writes_only = path.startswith(TESTS)
            for node in ast.walk(tree):
                attr = None
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id in names
                        and not (writes_only
                                 and not isinstance(node.ctx, ast.Store))):
                    attr = node.attr
                elif (not writes_only
                        and isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "getattr"
                        and len(node.args) >= 2
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id in names
                        and isinstance(node.args[1], ast.Constant)
                        and isinstance(node.args[1].value, str)):
                    attr = node.args[1].value
                if attr is not None and attr not in known:
                    missing.setdefault(attr, []).append(
                        "%s:%d" % (os.path.relpath(path, ROOT), node.lineno))
        self.assertEqual(
            missing, {},
            "these read a setting conf.Settings does not declare:\n  "
            + "\n  ".join("settings.%s at %s" % (k, ", ".join(v))
                          for k, v in sorted(missing.items())))


if __name__ == "__main__":
    unittest.main()
