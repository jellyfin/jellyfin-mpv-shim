"""A test that reads a repo file must say which encoding it is in.

`Path.read_text()` and `open()` use `locale.getencoding()`, which is UTF-8
on this project's Linux and macOS boxes and **cp1252 on Windows**. Five
files under `jellyfin_mpv_shim/` hold bytes cp1252 cannot decode, so a
source-walking test dies there with a `UnicodeDecodeError` that names a
byte offset and nothing else -- it reads as a broken tree rather than as a
test that forgot an argument.

Found by running the unit suite on the Windows VM, where
`test_hud_only_settings` was the one that happened to walk *all* of the
package. It had seventeen siblings, every one of them passing only because
the file it read was cp1252-decodable by luck: the failure is a property of
the file being read, not of the test doing the reading, so which of them
fails moves whenever somebody adds an em dash.

Scoped to `tests/` and `tools/`, which is where the repo reads its own
source. Application code that reads a *user's* file is a different
question with a different answer -- `conf.py` states UTF-8 and forgives a
BOM, and `docs/configuration.md` says so.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCANNED = ("tests", "tools")

#: Sites that read BYTES, or that are themselves this guard. Nothing is
#: listed here today; it exists so an exception is written down with a
#: reason rather than by dropping the directory from SCANNED.
ACCEPTED = {}


def _offenders():
    out = []
    for where in SCANNED:
        for path in sorted((ROOT / where).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                # Builtin `open` ONLY as a bare name. `Image.open`,
                # `tarfile.open` and `io.open`-alikes are not text reads,
                # and matching them by attribute name filled the first
                # draft's report with four calls that take no encoding at
                # all -- an allowlist of false positives is how a guard
                # teaches people to skim past it.
                if isinstance(func, ast.Attribute):
                    name = func.attr
                    if name not in ("read_text", "write_text"):
                        continue
                elif isinstance(func, ast.Name):
                    name = func.id
                    if name != "open":
                        continue
                else:
                    continue
                # `open(f, "rb")` and friends take no encoding, and an
                # `encoding=` anywhere in the call is the whole ask.
                if any(k.arg == "encoding" for k in node.keywords):
                    continue
                mode = next((a.value for a in node.args
                             if isinstance(a, ast.Constant)
                             and isinstance(a.value, str)
                             and set(a.value) <= set("rwxabt+")), "")
                if "b" in mode:
                    continue
                if rel in ACCEPTED:
                    continue
                out.append("%s:%d  %s(" % (rel, node.lineno, name))
    return out


class NoPlatformTextReadsTest(unittest.TestCase):
    def test_every_text_read_names_its_encoding(self):
        found = _offenders()
        self.assertEqual(
            found, [],
            "these read or write text in the platform's default encoding, "
            "which is cp1252 on Windows:\n  " + "\n  ".join(found)
            + "\n\nPass encoding=\"utf-8\". If the call really wants bytes, "
              "open it in binary mode; if it really wants the platform's "
              "encoding, add it to ACCEPTED with the reason.")

    def test_the_guard_can_actually_see_a_violation(self):
        """A lint that scans nothing passes for the wrong reason.

        Parses a violation and a clean call through the same code path, so
        "no offenders" is a measurement rather than an empty walk.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            probe = pathlib.Path(tmp) / "tests"
            probe.mkdir()
            (probe / "sample.py").write_text(
                "import pathlib\n"
                "a = pathlib.Path('x').read_text()\n"
                "b = pathlib.Path('y').read_text(encoding='utf-8')\n"
                "c = open('z', 'rb').read()\n"
                "d = Image.open('z.png')\n"
                "e = tarfile.open('z.tar')\n",
                encoding="utf-8")
            global ROOT
            was = ROOT
            try:
                ROOT = pathlib.Path(tmp)
                found = _offenders()
            finally:
                ROOT = was
        self.assertEqual(len(found), 1, found)
        self.assertIn("sample.py:2", found[0],
                      "the encoding-less read_text is what should be "
                      "reported; anything else means the matcher moved")


if __name__ == "__main__":
    unittest.main()
