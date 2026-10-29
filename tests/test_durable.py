"""durable.replace_file, and the loaders that recover from what it leaves.

Windows refuses `os.replace` onto a file another program has open (WinError
5) and still allows writing to it. The e2e suite's own polling of users.json
lost a profile switch that way; an antivirus scan or an indexer does the same
to a user. So a refused rename falls back to writing in place, with a
`.tmprec` of the previous contents made first and removed once the write
reads back. `.tmprec` is ours alone: a `.bak` may be the user's, and
users.json's `.bak` is its own rolling spare.
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
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from jellyfin_mpv_shim import durable

REFUSED = PermissionError(13, "Access is denied")
REC = ".tmprec"


class Crash(BaseException):
    """The process dying: not an Exception, so no `except Exception` in the
    code under test can swallow it."""


def torn_in_place():
    """Patch `durable._write` so the in-place write (not the temp file)
    stops half way, the way a power cut would leave it."""
    real = durable._write

    def write(path, text, encoding, mode=None):
        if path.endswith(".tmp"):
            return real(path, text, encoding, mode)
        with open(path, "w", encoding=encoding) as fh:
            fh.write(text[:len(text) // 2])
        raise Crash()
    return mock.patch.object(durable, "_write", write)


class ReplaceFileTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "x.json")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("old")

    def read(self, path=None):
        with open(path or self.path, encoding="utf-8") as fh:
            return fh.read()

    def debris(self):
        return sorted(n for n in os.listdir(self.dir) if n != "x.json")

    def test_the_ordinary_path_leaves_nothing_beside_the_file(self):
        durable.replace_file(self.path, "new")
        self.assertEqual("new", self.read())
        self.assertEqual([], self.debris())

    def test_a_refused_rename_writes_in_place_and_drops_the_backup(self):
        with mock.patch("os.replace", side_effect=REFUSED):
            durable.replace_file(self.path, "new")
        self.assertEqual("new", self.read())
        self.assertEqual([], self.debris(),
                         "the backup outlived a write that read back true")

    def test_a_successful_rename_clears_a_leftover_recovery_copy(self):
        with open(self.path + REC, "w", encoding="utf-8") as fh:
            fh.write("older")
        durable.replace_file(self.path, "new")
        self.assertEqual([], self.debris())

    def test_a_recovery_copy_already_there_is_not_overwritten(self):
        """One that exists is the last good copy of a write that never
        finished, and the file beside it may be torn: copying that over it
        and crashing again would leave nothing."""
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("torn")
        with open(self.path + REC, "w", encoding="utf-8") as fh:
            fh.write("good")
        with mock.patch("os.replace", side_effect=REFUSED), torn_in_place():
            with self.assertRaises(Crash):
                durable.replace_file(self.path, "new")
        self.assertEqual("good", self.read(self.path + REC))

    def test_the_backup_name_is_never_touched(self):
        """`.bak` is the user's, or users.json's own spare."""
        with open(self.path + ".bak", "w", encoding="utf-8") as fh:
            fh.write("theirs")
        for refuse in (False, True):
            with mock.patch("os.replace", side_effect=REFUSED if refuse
                            else os.replace):
                durable.replace_file(self.path, "new")
        self.assertEqual("theirs", self.read(self.path + ".bak"))

    def test_a_write_that_reads_back_wrong_raises_and_keeps_the_backup(self):
        real = durable._write

        def write(path, text, encoding, mode=None):
            real(path, text if path.endswith(".tmp") else "garbled",
                 encoding, mode)
        with mock.patch("os.replace", side_effect=REFUSED), \
                mock.patch.object(durable, "_write", write):
            with self.assertRaises(OSError):
                durable.replace_file(self.path, "new")
        self.assertEqual("old", self.read(self.path + REC))
        self.assertNotIn("x.json.tmp", self.debris())

    def test_no_backup_no_write(self):
        """The copy is what makes writing in place safe, so failing to make
        it must leave the file alone."""
        with mock.patch("os.replace", side_effect=REFUSED), \
                mock.patch("shutil.copyfileobj", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                durable.replace_file(self.path, "new")
        self.assertEqual("old", self.read())

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits")
    def test_mode_covers_the_temp_file_and_the_backup(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        modes = {}
        real = durable._write

        def write(path, text, encoding, mode=None):
            real(path, text, encoding, mode)
            for name in os.listdir(self.dir):
                modes[name] = os.stat(
                    os.path.join(self.dir, name)).st_mode & 0o777
        with mock.patch("os.replace", side_effect=REFUSED), \
                mock.patch.object(durable, "_write", write):
            durable.replace_file(self.path, "new", mode=0o600)
        self.assertEqual(0o600, modes["x.json.tmp"])
        self.assertEqual(0o600, modes["x.json" + REC])

    def test_a_refusal_with_no_file_there_is_not_papered_over(self):
        os.remove(self.path)
        with mock.patch("os.replace", side_effect=REFUSED):
            with self.assertRaises(PermissionError):
                durable.replace_file(self.path, "new")
        self.assertEqual([], self.debris())

    def test_another_process_holding_the_file_open(self):
        """The real thing. On Windows this is the WinError 5 path; elsewhere
        the rename succeeds and this is the ordinary one."""
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time\nf = open(sys.argv[1])\n"
             "print('open', flush=True)\ntime.sleep(30)", self.path],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual("open", holder.stdout.readline().strip())
        with mock.patch.object(durable, "_write_in_place",
                               wraps=durable._write_in_place) as in_place:
            for n in range(3):
                durable.replace_file(self.path, "new %d" % n)
                self.assertEqual("new %d" % n, self.read())
        self.assertEqual([], self.debris())
        # Which path ran is the premise, not a detail: on Windows the rename
        # must have been refused, or this test proved nothing there.
        self.assertEqual(os.name == "nt", in_place.called)


class TornWriteRecoveryTest(unittest.TestCase):
    """A crash half way through the in-place write: the file is torn, the
    `.bak` is whole, and the next launch must load the `.bak`."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_users_json(self):
        from jellyfin_mpv_shim.users import UserManager
        with mock.patch("jellyfin_mpv_shim.users.conffile.get",
                        side_effect=lambda app, f, create=False:
                        os.path.join(self.dir, f)):
            um = UserManager()
            um.load()
            um.add_user("Second")
            with mock.patch("os.replace", side_effect=REFUSED), \
                    torn_in_place():
                with self.assertRaises(Crash):
                    um.add_user("Third")
            with self.assertRaises(ValueError):
                with open(os.path.join(self.dir, "users.json"),
                          encoding="utf-8") as fh:
                    json.load(fh)                 # the premise: it is torn
            again = UserManager()
            again.load()
        self.assertEqual(["(default)", "Second"],
                         [u["name"] for u in again.users])

    def test_a_clean_users_json_load_drops_stale_recovery_copies(self):
        # Left by an in-place write whose open was refused; they hold tokens
        # and nothing reads them, since users.json recovers from its .bak.
        from jellyfin_mpv_shim.users import UserManager
        with mock.patch("jellyfin_mpv_shim.users.conffile.get",
                        side_effect=lambda app, f, create=False:
                        os.path.join(self.dir, f)):
            UserManager().load()
            stale = [os.path.join(self.dir, name + durable.RECOVERY_SUFFIX)
                     for name in ("users.json", "users.json.bak")]
            for p in stale:
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write('{"old": "token"}')
            UserManager().load()
        self.assertEqual([], [p for p in stale if os.path.exists(p)])

    def test_conf_json(self):
        from jellyfin_mpv_shim.conf import Settings
        path = os.path.join(self.dir, "conf.json")
        s = Settings()
        self.assertTrue(s.load(path))
        s.player_name = "before"
        self.assertTrue(s.save())
        with mock.patch("os.replace", side_effect=REFUSED), torn_in_place():
            s.player_name = "after"
            with self.assertRaises(Crash):
                s.save()
        again = Settings()
        self.assertTrue(again.load(path, create=False),
                        "a torn conf.json was not restored from its recovery copy")
        self.assertEqual("before", again.player_name)
        self.assertTrue(any(n.startswith("conf.json.unreadable-")
                            for n in os.listdir(self.dir)),
                        "the unreadable file was not kept")

    def _conf(self, body, **siblings):
        """conf.json holding ``body``; each sibling is a suffix -> text,
        e.g. ``rec=...`` for conf.json.tmprec, ``bak=...`` for .bak."""
        path = os.path.join(self.dir, "conf.json")
        names = {"rec": REC, "bak": ".bak"}
        files = [(path, body)] + [(path + names[k], v)
                                  for k, v in siblings.items()]
        for p, data in files:
            with open(p, "wb") as fh:
                fh.write(data.encode("utf-8") if isinstance(data, str)
                         else data)
        return path

    def test_a_conf_json_that_parses_is_never_swapped_for_its_backup(self):
        """Only torn bytes are restored. A file that parses and then fails
        in our own code (a migration bug) is the user's current config, and
        an older copy must not replace it."""
        from jellyfin_mpv_shim.conf import Settings
        body = '{"player_name": "current"}'
        old = '{"player_name": "old"}'
        path = self._conf(body, rec=old, bak=old)
        with mock.patch.object(Settings, "_migrate",
                               side_effect=RuntimeError("migration bug")):
            self.assertFalse(Settings().load(path, create=False))
        self.assertEqual(body, self.read_file(path))
        self.assertEqual(["conf.json", "conf.json.bak", "conf.json" + REC],
                         sorted(os.listdir(self.dir)))

    def test_a_recovery_copy_that_is_not_a_settings_object_is_not_used(self):
        from jellyfin_mpv_shim.conf import Settings
        path = self._conf('{"player_name": "half', rec="[]", bak="[]")
        self.assertFalse(Settings().load(path, create=False))
        self.assertEqual('{"player_name": "half', self.read_file(path))

    def test_a_users_own_conf_json_bak_is_never_used(self):
        from jellyfin_mpv_shim.conf import Settings
        path = self._conf('{"player_name": "half',
                          bak='{"player_name": "theirs"}')
        self.assertFalse(Settings().load(path, create=False))
        self.assertEqual('{"player_name": "half', self.read_file(path))
        self.assertEqual('{"player_name": "theirs"}',
                         self.read_file(path + ".bak"))

    def test_an_undecodable_conf_json_is_never_replaced(self):
        """We write pure ASCII, so bytes that do not decode are a hand edit
        (Notepad's ANSI), never a write of ours that was cut short."""
        from jellyfin_mpv_shim.conf import Settings
        body = '{"sync_path": "D:/Filme/\u00dcbersicht"}'.encode("cp1252")
        good = '{"player_name": "older"}'
        path = self._conf(body, rec=good, bak=good)
        self.assertFalse(Settings().load(path, create=False))
        with open(path, "rb") as fh:
            self.assertEqual(body, fh.read())

    def test_a_torn_conf_json_comes_back_and_leaves_no_recovery_copy(self):
        from jellyfin_mpv_shim.conf import Settings
        path = self._conf('{"player_name": "ha',
                          rec='{"player_name": "before"}')
        s = Settings()
        self.assertTrue(s.load(path, create=False))
        self.assertEqual("before", s.player_name)
        names = os.listdir(self.dir)
        self.assertNotIn("conf.json" + REC, names,
                         "a used recovery copy stayed to be used again")
        self.assertTrue(any(n.startswith("conf.json.unreadable-")
                            for n in names))

    def test_a_refused_restore_leaves_conf_json_where_it_was(self):
        """If the recovery copy cannot be put in place, conf.json must still
        be there: a missing one reads as a first run, which writes defaults."""
        from jellyfin_mpv_shim.conf import Settings
        good = '{"player_name": "before"}'
        path = self._conf('{"player_name": "ha', rec=good, bak=good)
        real_replace, real_copy = os.replace, shutil.copyfile

        def refuse_into(real):
            def call(src, dst, *a, **k):
                if os.path.abspath(dst) == os.path.abspath(path):
                    raise REFUSED
                return real(src, dst, *a, **k)
            return call
        with mock.patch("os.replace", refuse_into(real_replace)), \
                mock.patch("shutil.copyfile", refuse_into(real_copy)):
            self.assertFalse(Settings().load(path, create=False))
        self.assertEqual('{"player_name": "ha', self.read_file(path))
        self.assertEqual(good, self.read_file(path + REC))

    def test_a_clean_load_clears_a_stale_recovery_copy(self):
        """conf.json loaded, so a recovery copy beside it is from a write
        that did finish. Left there, a later hand-edit typo would bring the
        old settings back."""
        from jellyfin_mpv_shim.conf import Settings
        path = self._conf('{"player_name": "now"}',
                          rec='{"player_name": "old"}')
        self.assertTrue(Settings().load(path, create=False))
        self.assertNotIn("conf.json" + REC, os.listdir(self.dir))

    def test_deep_nesting_is_unreadable_not_a_crash(self):
        """load() runs before logging is up; anything json raises has to
        end in "unreadable", including RecursionError."""
        from jellyfin_mpv_shim.conf import Settings
        deep = '{"x": ' + "[" * 100000 + "]" * 100000 + "}"
        for body, rec in ((deep, None), ('{"player_name": "ha', deep)):
            with self.subTest(primary_is_deep=rec is None):
                for n in os.listdir(self.dir):
                    os.remove(os.path.join(self.dir, n))
                siblings = {} if rec is None else {"rec": rec, "bak": rec}
                path = self._conf(body, **siblings)
                self.assertFalse(Settings().load(path, create=False))

    @staticmethod
    def read_file(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_a_torn_conf_json_with_no_backup_is_still_unreadable(self):
        """The control: with nothing to restore, the existing guard holds
        -- load says no, and save will not write defaults over the file."""
        from jellyfin_mpv_shim.conf import Settings
        path = os.path.join(self.dir, "conf.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"player_name": "half')
        s = Settings()
        self.assertFalse(s.load(path, create=False))
        self.assertFalse(s.save())

    def test_shader_overrides_clear_a_stale_recovery_copy(self):
        from jellyfin_mpv_shim import shader_overrides
        path = os.path.join(self.dir, "shader_profiles.json")
        shader_overrides.ShaderOverrides(path).set("series", "s1", "fsr")
        with open(path + REC, "w", encoding="utf-8") as fh:
            fh.write('{"series": {"s1": "anime4k"}}')
        again = shader_overrides.ShaderOverrides(path)
        self.assertEqual("fsr", again.get("series", "s1"))
        self.assertNotIn("shader_profiles.json" + REC, os.listdir(self.dir))

    def test_shader_overrides(self):
        from jellyfin_mpv_shim import shader_overrides
        path = os.path.join(self.dir, "shader_profiles.json")
        store = shader_overrides.ShaderOverrides(path)
        store.set("series", "s1", "anime4k")
        with mock.patch("os.replace", side_effect=REFUSED), torn_in_place():
            with self.assertRaises(Crash):
                store.set("series", "s2", "fsr")
        again = shader_overrides.ShaderOverrides(path)
        self.assertEqual("anime4k", again.get("series", "s1"))


if __name__ == "__main__":
    unittest.main()
