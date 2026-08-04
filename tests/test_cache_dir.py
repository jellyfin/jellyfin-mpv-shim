"""Where the scratch caches go, and who cleans them up.

Both halves of one field report (#3): ``XDG_RUNTIME_DIR`` filled up after a
few runs. That is two bugs wearing one symptom.

**Nobody swept.** The only cleanup was an ``atexit`` hook, which covers the
one ending a media player rarely gets -- a clean exit. A crash, a SIGKILL,
a window manager killing the session, or a test run that times out leaves
the whole directory behind, and the next run adds another.

**Nobody looked.** ``XDG_RUNTIME_DIR`` is a tmpfs sized from RAM and shared
with the rest of the login session, so filling it does not just evict our
own pictures: it is ENOSPC for pipewire, gvfs, dbus and systemd too.

So the base is now chosen by what is free *after* sweeping what previous
runs abandoned, and a base without room to spare loses to ~/.cache on real
disk. These tests drive that choice with fake bases, because the real
answer depends on the machine the suite happens to run on.
"""

import os
import shutil
import tempfile
import unittest

from jellyfin_mpv_shim.mpvtk import rawimage


class SweepTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="jms-sweep-")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    def _dir(self, name, mtime=None):
        path = os.path.join(self.base, name)
        os.makedirs(path)
        with open(os.path.join(path, "a.img"), "wb") as fh:
            fh.write(b"x" * 16)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_a_dead_sessions_cache_is_reclaimed(self):
        # A pid that cannot be running: one past the system maximum.
        dead = self._dir("mpvtk-thumbs-%d-abc" % (2 ** 22 + 7))
        rawimage.sweep_stale(self.base, "mpvtk-thumbs-")
        self.assertFalse(os.path.exists(dead))

    def test_a_live_sessions_cache_is_left_alone(self):
        mine = self._dir("mpvtk-thumbs-%d-abc" % os.getpid())
        rawimage.sweep_stale(self.base, "mpvtk-thumbs-")
        self.assertTrue(os.path.exists(mine),
                        "swept the cache of a session that is still running")

    def test_another_prefix_is_not_ours_to_remove(self):
        # Several stores share a base with different prefixes, and so do
        # other programs; only the prefix asked for is in scope.
        other = self._dir("mpvtk-browser-%d-abc" % (2 ** 22 + 7))
        rawimage.sweep_stale(self.base, "mpvtk-thumbs-")
        self.assertTrue(os.path.exists(other))

    def test_a_nameless_leftover_goes_by_age(self):
        # Dirs from before the pid was in the name. There is nothing to ask
        # about them, so they are reclaimed only once nothing could still be
        # writing to them.
        fresh = self._dir("mpvtk-thumbs-abcdef")
        old = self._dir("mpvtk-thumbs-ghijkl",
                        mtime=1_000_000)          # 1970, comfortably stale
        rawimage.sweep_stale(self.base, "mpvtk-thumbs-")
        self.assertTrue(os.path.exists(fresh))
        self.assertFalse(os.path.exists(old))

    def test_a_file_that_only_looks_like_one_is_untouched(self):
        path = os.path.join(self.base, "mpvtk-thumbs-1-notadir")
        with open(path, "wb") as fh:
            fh.write(b"x")
        rawimage.sweep_stale(self.base, "mpvtk-thumbs-")
        self.assertTrue(os.path.exists(path))


class BaseChoiceTest(unittest.TestCase):
    """``cache_dir`` picking between RAM and disk."""

    def setUp(self):
        self.ram = tempfile.mkdtemp(prefix="jms-ram-")
        self.disk = tempfile.mkdtemp(prefix="jms-disk-")
        for path in (self.ram, self.disk):
            self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        self._bases = rawimage._bases
        self._usage = shutil.disk_usage
        self.addCleanup(setattr, rawimage, "_bases", self._bases)
        self.addCleanup(setattr, shutil, "disk_usage", self._usage)
        rawimage._bases = lambda: [self.ram, self.disk]

    def _free(self, mapping):
        def usage(path):
            return type("U", (), {"free": mapping.get(path, 1 << 40)})
        shutil.disk_usage = usage

    def _make(self, **kw):
        path = rawimage.cache_dir(prefix="jms-test-", **kw)
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def test_ram_wins_when_it_has_room(self):
        self._free({})
        self.assertTrue(self._make().startswith(self.ram + os.sep))

    def test_a_tight_tmpfs_loses_to_the_disk_cache(self):
        self._free({self.ram: 4 * 1024 * 1024})
        path = self._make()
        self.assertTrue(path.startswith(self.disk + os.sep),
                        "cached into a tmpfs with 4 MiB free")

    def test_space_a_dead_session_is_holding_counts_as_free(self):
        # The sweep runs before the measurement on purpose: the space this
        # app leaked on its last run is exactly the space that makes the
        # tmpfs look too full to use on this one.
        dead = os.path.join(self.ram, "jms-test-%d-abc" % (2 ** 22 + 7))
        os.makedirs(dead)
        self._free({})
        self._make()
        self.assertFalse(os.path.exists(dead))

    def test_the_name_says_who_owns_it(self):
        # Which is the whole basis of the sweep above.
        self._free({})
        name = os.path.basename(self._make())
        self.assertTrue(name.startswith("jms-test-%d-" % os.getpid()), name)

    def test_two_dirs_for_one_process_do_not_collide(self):
        self._free({})
        self.assertNotEqual(self._make(), self._make())

    def test_a_missing_variable_does_not_end_the_search(self):
        # An unset XDG_RUNTIME_DIR is every macOS login and plenty of Linux
        # ones. Treating it as the end of the list would send those straight
        # to the system temp dir, never trying /dev/shm or ~/.cache.
        rawimage._bases = lambda: [os.path.join(self.ram, "absent"),
                                   self.disk]
        self._free({})
        self.assertTrue(self._make().startswith(self.disk + os.sep))

    def test_running_out_of_candidates_falls_back_to_tempfile(self):
        rawimage._bases = lambda: []
        self._free({})
        self.assertTrue(self._make().startswith(tempfile.gettempdir()))


class CacheLocationTest(unittest.TestCase):
    """``conffile.cachedir`` — regenerable data, which is not configuration.

    Separate directories because they have opposite lifetimes: config is the
    user's and gets backed up, a cache is the app's and is safe to delete at
    any moment. Every platform has somewhere it already knows it may reclaim.
    """

    def setUp(self):
        from jellyfin_mpv_shim import conffile

        self.conffile = conffile
        self._env = {k: os.environ.get(k)
                     for k in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME")}
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_it_is_the_xdg_cache_directory_not_the_config_one(self):
        os.environ["XDG_CACHE_HOME"] = "/x/cache"
        os.environ["XDG_CONFIG_HOME"] = "/x/config"
        got = self.conffile.posix_cache("app")
        self.assertEqual(got, os.path.join("/x/cache", "app"))
        self.assertNotIn("/x/config", got)

    def test_it_falls_back_to_the_config_directory_not_to_nothing(self):
        # There is no case where this app can keep credentials but not
        # artwork: an unknown platform puts both in the same place rather
        # than sending the cache to scratch space it will lose on exit.
        saved = self.conffile._cachedir
        self.addCleanup(setattr, self.conffile, "_cachedir", saved)
        self.conffile._cachedir = None
        self.assertEqual(
            self.conffile.cachedir("app"),
            os.path.join(self.conffile.confdir("app"), "cache"))

    def test_windows_uses_the_local_profile_not_the_roaming_one(self):
        # A cache must not follow a roaming profile around a domain.
        os.environ["LOCALAPPDATA"] = r"C:\Users\x\AppData\Local"
        self.addCleanup(os.environ.pop, "LOCALAPPDATA", None)
        self.assertIn("Local", self.conffile.win32_cache("app"))
        self.assertNotIn("Roaming", self.conffile.win32_cache("app"))


class PlatformScratchTest(unittest.TestCase):
    """Scratch space is an XDG idea. The other two platforms have no tmpfs
    to prefer and no ~/.cache to speak of, and offering them one would move
    files that used to live in the system temp directory -- which matters
    most on macOS, where mpv_ext is forced, so the strip cache is FILES."""

    def _bases_on(self, platform):
        import sys
        from unittest import mock

        with mock.patch.object(sys, "platform", platform):
            return rawimage._bases()

    def test_windows_and_macos_leave_it_to_tempfile(self):
        for platform in ("win32", "darwin"):
            with self.subTest(platform=platform):
                self.assertEqual(self._bases_on(platform), [])

    def test_linux_prefers_ram(self):
        os.environ["XDG_RUNTIME_DIR"] = "/run/user/1234"
        self.addCleanup(os.environ.pop, "XDG_RUNTIME_DIR", None)
        bases = self._bases_on("linux")
        self.assertEqual(bases[0], "/run/user/1234")
        self.assertIn("/dev/shm", bases)
