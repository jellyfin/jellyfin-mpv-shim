"""The config file is read with a stated encoding, because a person edits it.

`docs/configuration.md` documents hand-editing `conf.json`, and a hand-typed
value is exactly where a non-ASCII character enters: a `sync_path` under
`D:/Filme/Übersicht`, an `mpv_ext_path` inside a profile directory with an
umlaut. An editor writes those as UTF-8 -- often with a BOM on Windows -- and
`open()` with no `encoding=` decodes with the *locale's* codec, which on a
Western Windows install is cp1252.

Two different failures, and neither is visible from a UTF-8 machine:

- **A BOM**: it decodes fine and then `json.loads` rejects it, which
  `load()` catches, so the whole file is discarded with "not valid JSON" and
  the app runs on defaults.
- **A non-ASCII byte under a non-UTF-8 locale**: `UnicodeDecodeError` from
  `fh.read()`, which is outside the `except` and is not caught by
  `mpv_shim.main` either -- an unhandled traceback before anything is on
  screen.

Reading as `utf-8-sig` answers both: identical to utf-8 except that it
tolerates the BOM the editor added.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import json
import os
import subprocess
import sys
import tempfile
import unittest

from jellyfin_mpv_shim.conf import Settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: A path a person would plausibly type, and cannot be spelled in ASCII.
NON_ASCII_PATH = "D:/Filme/Übersicht"


class ConfigFileEncodingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "conf.json")

    def _write(self, text, encoding="utf-8"):
        with open(self.path, "w", encoding=encoding) as fh:
            fh.write(text)

    def _body(self, **extra):
        return json.dumps(dict({"sync_path": NON_ASCII_PATH}, **extra),
                          ensure_ascii=False, indent=4)

    def test_a_hand_edited_file_with_a_bom_is_not_discarded(self):
        """What Notepad leaves behind. The bytes are valid UTF-8 and the JSON
        is valid; only the BOM stands between the user and their settings."""
        self._write(self._body(), encoding="utf-8-sig")
        s = Settings()
        self.assertTrue(
            s.load(self.path, create=False),
            "a config saved with a BOM was rejected as invalid JSON, so "
            "every setting in it was silently ignored")
        self.assertEqual(s.sync_path, NON_ASCII_PATH)

    def test_a_utf8_value_survives_a_non_utf8_locale(self):
        """In a subprocess, because the locale's codec is fixed at startup and
        this machine's is UTF-8 -- in process the bug is unreachable and the
        test would pass over it.

        `LC_ALL=C` plus `PYTHONUTF8=0`/`PYTHONCOERCECLOCALE=0` is the closest
        a Linux box gets to the Windows cp1252 default: `open()` without an
        encoding then decodes as ASCII.
        """
        self._write(self._body())
        prog = (
            "import sys, json;"
            "sys.path.insert(0, %r);"
            "sys.argv = [sys.argv[0]];"
            "from jellyfin_mpv_shim.conf import Settings;"
            "s = Settings();"
            "ok = s.load(%r, create=False);"
            "print(json.dumps({'ok': ok, 'sync_path': s.sync_path}))"
            % (ROOT, self.path))
        env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONUTF8="0",
                   PYTHONCOERCECLOCALE="0", PYTHONIOENCODING="utf-8")
        env.pop("PYTHONPATH", None)
        proc = subprocess.run([sys.executable, "-c", prog], env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(
            proc.returncode, 0,
            "loading the config crashed under a non-UTF-8 locale -- which is "
            "an unhandled traceback at startup, before anything is on "
            "screen:\n%s" % proc.stderr[-2000:])
        answer = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(answer["ok"], "the config was discarded")
        self.assertEqual(answer["sync_path"], NON_ASCII_PATH,
                         "the path came back mangled")

    def test_the_locale_probe_itself_is_honest(self):
        """Guard on the guard: if the subprocess above quietly ran in UTF-8
        mode, that test would pass whatever the code did."""
        prog = ("import locale, sys;"
                "print(locale.getpreferredencoding(False))")
        env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONUTF8="0",
                   PYTHONCOERCECLOCALE="0")
        env.pop("PYTHONPATH", None)
        proc = subprocess.run([sys.executable, "-c", prog], env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertNotIn("utf", proc.stdout.strip().lower(),
                         "the subprocess still defaulted to UTF-8, so the "
                         "locale test above cannot fail")

    def test_a_saved_config_reads_back_under_that_locale_too(self):
        """The pair, not one end: `save()` must write what `load()` reads.
        `json.dump` escapes non-ASCII by default, so this passes today -- it
        is here so that a future `ensure_ascii=False` cannot pass alone."""
        s = Settings()
        s.load(self.path, create=True)
        s.sync_path = NON_ASCII_PATH
        self.assertTrue(s.save())
        with open(self.path, "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"\xc3\x9c", raw,
                         "the saved config carries raw UTF-8, so it can only "
                         "be read back where the locale agrees")


if __name__ == "__main__":
    unittest.main()
