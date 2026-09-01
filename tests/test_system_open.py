"""Handing a file to the desktop.

Nothing here launches anything: every test replaces the two functions that
touch the outside world (``shutil.which`` and ``subprocess.Popen``) and
asserts on what would have been run. A test that really opened a book would
leave a reader on the developer's screen, which is the same reason the
browser suite runs under xvfb.

The behaviour worth pinning is mostly about *failing usefully*. This is the
last step of reading a book, so "nothing happened" is the outcome the user
sees, and the three ways to get there — no file, no opener, a launcher that
would not start — have to be distinguishable to the caller.
"""

import os
import subprocess
import tempfile
import unittest

from jellyfin_mpv_shim import system_open


class Harness(unittest.TestCase):

    def setUp(self):
        self.spawned = []
        self.available = set()
        self.fail_spawn = False

        def fake_popen(argv, **kwargs):
            self.spawned.append((list(argv), kwargs))
            if self.fail_spawn:
                raise OSError("no such binary")
            return object()

        def fake_which(name):
            return "/usr/bin/" + name if name in self.available else None

        self._patch(subprocess, "Popen", fake_popen)
        self._patch(system_open.shutil, "which", fake_which)
        fh = tempfile.NamedTemporaryFile(suffix=".epub", delete=False)
        fh.close()
        self.book = fh.name
        self.addCleanup(os.unlink, self.book)

    def _patch(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def _platform(self, os_name, sys_platform):
        self._patch(system_open.os, "name", os_name)
        self._patch(system_open.sys, "platform", sys_platform)


class LinuxTest(Harness):

    def setUp(self):
        super().setUp()
        self._platform("posix", "linux")

    def test_xdg_open_is_preferred(self):
        self.available = {"xdg-open", "gio", "kde-open"}
        ok, method = system_open.open_path(self.book)
        self.assertEqual((ok, method), (True, "xdg-open"))
        self.assertEqual(self.spawned[0][0], ["xdg-open", self.book])

    def test_it_falls_through_to_what_is_installed(self):
        # A session without xdg-utils is unusual but real, and the
        # desktop-specific launchers are what xdg-open would have delegated
        # to anyway.
        self.available = {"kde-open"}
        ok, method = system_open.open_path(self.book)
        self.assertEqual((ok, method), (True, "kde-open"))

    def test_gio_takes_a_subcommand(self):
        # The one opener here that is not "<command> <file>". Getting this
        # wrong makes gio print its usage and exit 0, which reads as a
        # successful open of nothing.
        self.available = {"gio"}
        system_open.open_path(self.book)
        self.assertEqual(self.spawned[0][0], ["gio", "open", self.book])

    def test_nothing_installed_is_a_clean_refusal(self):
        self.available = set()
        self.assertEqual(system_open.open_path(self.book), (False, None))
        self.assertEqual(self.spawned, [])

    def test_a_launcher_that_will_not_start_is_skipped(self):
        # Popen raising is a launcher that is on PATH but unusable (a broken
        # symlink, a wrapper script with no interpreter). Try the next one
        # rather than reporting success.
        self.fail_spawn = True
        self.available = {"xdg-open", "gio"}
        self.assertEqual(system_open.open_path(self.book), (False, None))
        self.assertEqual([argv[0] for argv, _kw in self.spawned],
                         ["xdg-open", "gio"])

    def test_the_reader_gets_its_own_session(self):
        # A reader outlives the shim, and a terminal-based handler must not
        # inherit our stdin. Both are one kwarg, and neither is visible in
        # any output if it is missing.
        self.available = {"xdg-open"}
        system_open.open_path(self.book)
        _argv, kwargs = self.spawned[0]
        self.assertTrue(kwargs.get("start_new_session"))
        self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertEqual(kwargs.get("stdout"), subprocess.DEVNULL)
        self.assertEqual(kwargs.get("stderr"), subprocess.DEVNULL)

    def test_nothing_is_waited_on(self):
        # Popen, never run/communicate/wait. A reader is a GUI application
        # that lives as long as the user is reading; waiting for it would
        # hold a worker for the duration.
        self.available = {"xdg-open"}
        ran = []
        self._patch(subprocess, "run", lambda *a, **k: ran.append(a))
        system_open.open_path(self.book)
        self.assertEqual(ran, [])


class MissingFileTest(Harness):

    def setUp(self):
        super().setUp()
        self._platform("posix", "linux")
        self.available = {"xdg-open"}

    def test_a_file_that_is_gone_is_not_handed_over(self):
        # Its own answer, and no launch. A missing file means the download
        # went away -- the store was moved or pruned -- and passing the path
        # on would have the desktop report a corrupt book instead.
        os.unlink(self.book)
        self.addCleanup(lambda: open(self.book, "w", encoding="utf-8").close())
        self.assertEqual(system_open.open_path(self.book), (False, None))
        self.assertEqual(self.spawned, [])

    def test_no_path_at_all_is_refused(self):
        self.assertEqual(system_open.open_path(None), (False, None))
        self.assertEqual(system_open.open_path(""), (False, None))
        self.assertEqual(self.spawned, [])


class MacTest(Harness):

    def test_open_is_used(self):
        self._platform("posix", "darwin")
        # Deliberately not gated on `which`: `open` is part of the OS, and
        # probing for it would be a way to fail on a machine where it is
        # certainly present.
        self.available = set()
        ok, method = system_open.open_path(self.book)
        self.assertEqual((ok, method), (True, "open"))
        self.assertEqual(self.spawned[0][0], ["open", self.book])


class WindowsTest(Harness):

    def test_startfile_is_used(self):
        self._platform("nt", "win32")
        called = []
        # os.startfile does not exist on this platform, so it is installed
        # rather than replaced -- which is also why the production call sites
        # it and the branch guarding it have to agree.
        system_open.os.startfile = lambda path: called.append(path)
        self.addCleanup(lambda: delattr(system_open.os, "startfile"))
        ok, method = system_open.open_path(self.book)
        self.assertEqual((ok, method), (True, "startfile"))
        self.assertEqual(called, [self.book])
        self.assertEqual(self.spawned, [])

    def test_a_failing_startfile_is_reported(self):
        self._platform("nt", "win32")

        def boom(_path):
            raise OSError("no association")

        system_open.os.startfile = boom
        self.addCleanup(lambda: delattr(system_open.os, "startfile"))
        self.assertEqual(system_open.open_path(self.book), (False, None))


if __name__ == "__main__":
    unittest.main()
