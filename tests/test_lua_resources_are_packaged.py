"""Every lua script the app loads must reach every build that ships it.

**Three places, not one**, and only the first is a glob.

`pyproject.toml` globs `*.lua` into package-data, which covers the wheel.
The sdist is `MANIFEST.in`'s and the Windows builds are the four
`build-win*.bat` PyInstaller lines, and **both list the scripts one by
one**. A new script is therefore four more edits, every one of them
invisible from a checkout: it works when you run it, it works from a wheel,
and it is missing from an sdist and from every Windows build.

Which matters more than it sounds. `lua_probe.lua` decides whether the shim
believes mpv can run lua at all; absent, the probe times out and the app
drops to the CLI with the whole GUI disabled. It was added with none of
those five lines, and nothing would have said so — **[iw]** caught the
batch scripts by memory.
"""

import os
import re
import sys
import unittest

sys.argv = [sys.argv[0]]

import jellyfin_mpv_shim                                        # noqa: E402

BASE = os.path.dirname(os.path.abspath(jellyfin_mpv_shim.__file__))
ROOT = os.path.dirname(BASE)


def _manifest():
    with open(os.path.join(ROOT, "MANIFEST.in"), encoding="utf-8") as fh:
        return fh.read()


def _win_builds():
    """``{filename: text}`` for the PyInstaller build scripts."""
    out = {}
    for name in sorted(os.listdir(ROOT)):
        if name.startswith("build-win") and name.endswith(".bat"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                out[name] = fh.read()
    return out


def _shipped_lua():
    """Every .lua under the package, relative to the repo root."""
    out = []
    for dirpath, _dirs, files in os.walk(BASE):
        for name in files:
            if name.endswith(".lua"):
                out.append(os.path.relpath(os.path.join(dirpath, name), ROOT))
    return sorted(out)


class LuaIsPackagedTest(unittest.TestCase):
    def test_every_lua_file_is_in_the_manifest(self):
        manifest = _manifest()
        missing = [p for p in _shipped_lua()
                   if p.replace(os.sep, "/") not in manifest]
        self.assertEqual(
            missing, [],
            "these ship in a checkout and a wheel but NOT in an sdist -- "
            "add `include <path>` to MANIFEST.in: %s" % missing)

    def test_every_lua_file_is_in_every_windows_build(self):
        """PyInstaller bundles what it is told to, one --add-data at a
        time. A missing one is a build that runs and silently has no
        script."""
        builds = _win_builds()
        self.assertGreaterEqual(len(builds), 4, "found no build scripts")
        for name, text in builds.items():
            for path in _shipped_lua():
                base = os.path.basename(path)
                with self.subTest(build=name, lua=base):
                    self.assertIn(
                        base, text,
                        "%s does not bundle %s, so that build ships "
                        "without it" % (name, base))

    def test_the_scan_actually_finds_them(self):
        # A guard on the guard: a broken walk would make the check vacuous.
        found = _shipped_lua()
        self.assertGreaterEqual(len(found), 4)
        self.assertTrue(any(p.endswith("lua_probe.lua") for p in found))
        self.assertTrue(any(p.endswith("renderer.lua") for p in found))

    def test_every_loaded_resource_exists(self):
        """...and the other half: a get_resource() naming a file that is not
        there fails the same silent way, because load-script raises nothing
        on either backend."""
        names = set()
        for dirpath, _dirs, files in os.walk(BASE):
            for name in files:
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(dirpath, name), encoding="utf-8",
                          errors="replace") as fh:
                    names.update(re.findall(r'get_resource\(\s*"([^"]+\.lua)"',
                                            fh.read()))
        self.assertTrue(names, "found no get_resource lua calls to check")
        for name in sorted(names):
            with self.subTest(name=name):
                self.assertTrue(os.path.isfile(os.path.join(BASE, name)),
                                "%s is loaded but not shipped" % name)


if __name__ == "__main__":
    unittest.main()
