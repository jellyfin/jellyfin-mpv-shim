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
import stat
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


class NamespacedSweepTest(unittest.TestCase):
    """Scratch caches live in a directory of their own, per configuration.

    Windows is what forces the question: ``os.kill(pid, 0)`` terminates
    rather than probes, so every abandoned directory looked alive forever
    and nothing was ever reclaimed there.

    The single-instance lock is what licenses reclaiming -- but only at the
    scope the lock has. It lives on a file inside the CONFIG directory while
    scratch space is machine-wide, so two copies started with different
    ``--config`` directories coexist quite legally and share a %TEMP%. Give
    each configuration its own directory and the claim becomes structural:
    the only live process that may write in there is this one.
    """

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="jms-ns-")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.addCleanup(rawimage.set_instance_namespace, None)
        original = rawimage._bases
        self.addCleanup(setattr, rawimage, "_bases", original)
        rawimage._bases = lambda: [self.base]

    def _make(self, prefix="jms-thumbs-"):
        path = rawimage.cache_dir(prefix=prefix, min_free=0)
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def _dir(self, *parts):
        path = os.path.join(self.base, *parts)
        os.makedirs(path)
        return path

    #: A pid that resolves to a live process and is not ours -- which is
    #: what EVERY pid looks like on Windows.
    LIVE_PID = 1

    def test_the_cache_goes_in_the_configurations_own_directory(self):
        rawimage.set_instance_namespace("app.cfg1")
        path = self._make()
        self.assertEqual(os.path.basename(os.path.dirname(path)), "app.cfg1")

    def test_a_dead_copy_of_my_configuration_is_reclaimed(self):
        # Even though the pid still resolves to a live process. No liveness
        # probe is involved, which is the point: there is nothing to ask on
        # Windows.
        rawimage.set_instance_namespace("app.cfg1")
        stale = self._dir("app.cfg1", "jms-thumbs-%d-abc" % self.LIVE_PID)
        self._make()
        self.assertFalse(os.path.exists(stale))

    def test_another_configuration_is_not_mine_to_reclaim(self):
        # The pair that makes "I hold the lock, so everything is dead" wrong.
        rawimage.set_instance_namespace("app.cfg1")
        theirs = self._dir("app.cfg2", "jms-thumbs-%d-abc" % self.LIVE_PID)
        self._make()
        self.assertTrue(os.path.exists(theirs),
                        "deleted a live second instance's cache")

    def test_every_prefix_goes_at_once(self):
        # Not just the one the store asking happens to use. On libmpv the
        # strip cache creates no directory at all, so a previous run's would
        # otherwise never be swept by anybody.
        rawimage.set_instance_namespace("app.cfg1")
        strips = self._dir("app.cfg1", "jms-browser-%d-abc" % self.LIVE_PID)
        self._make("jms-thumbs-")
        self.assertFalse(os.path.exists(strips))

    def test_my_own_directories_survive(self):
        rawimage.set_instance_namespace("app.cfg1")
        first = self._make("jms-thumbs-")
        second = self._make("jms-browser-")
        self.assertTrue(os.path.exists(first),
                        "swept a directory this very process is using")
        self.assertTrue(os.path.exists(second))

    def test_without_a_namespace_nothing_is_claimed_by_ownership(self):
        # Tests, the demo, an embedder: no lock, so the only evidence is the
        # pid, exactly as before.
        rawimage.set_instance_namespace(None)
        other = self._dir("jms-thumbs-%d-abc" % self.LIVE_PID)
        self._make()
        self.assertTrue(os.path.exists(other))


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


class OwnDirectoriesAreNeverSweptTest(unittest.TestCase):
    """Belt and braces over every ownership argument above.

    The pid rules decide whose a directory is by parsing a NAME, and a name
    is a weak thing to bet a `shutil.rmtree` on -- one prefix change away
    from a process deleting the cache it is writing to. Whatever this run
    actually created is off limits by identity, not by inference.
    """

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="jms-own-")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.addCleanup(rawimage.set_instance_namespace, None)
        original = rawimage._bases
        self.addCleanup(setattr, rawimage, "_bases", original)
        rawimage._bases = lambda: [self.base]

    def test_a_directory_this_run_made_survives_an_unparsable_name(self):
        rawimage.set_instance_namespace("app.cfg1")
        path = rawimage.cache_dir(prefix="jms-x-", min_free=0)
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        # Pretend the naming scheme changed under us: nothing about this
        # name says it is ours any more.
        moved = os.path.join(os.path.dirname(path), "unrecognisable")
        os.rename(path, moved)
        rawimage._ours.discard(path)
        rawimage._ours.add(moved)
        self.addCleanup(rawimage._ours.discard, moved)
        rawimage.sweep_stale(os.path.dirname(moved), "jms-x-")
        self.assertTrue(os.path.exists(moved))


class WhatMayBeSweptTest(unittest.TestCase):
    """The two ways this could be pointed at somebody else's directory.

    Everything above is about not deleting a *cache* that is still in use.
    This is the harder question a `shutil.rmtree` running on every startup
    has to answer: how does it know the directory it is emptying has
    anything to do with this app at all? Scratch space is shared -- /tmp and
    /dev/shm are writable by every user on the machine, and ~/.cache is full
    of other programs' state.
    """

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="jms-scope-")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.addCleanup(rawimage.set_instance_namespace, None)
        original = rawimage._bases
        self.addCleanup(setattr, rawimage, "_bases", original)
        rawimage._bases = lambda: [self.base]

    def _namespace(self, name):
        """Name this instance's namespace, and clean up after the copy of it
        that lands in the system temp dir when the fake base is refused --
        every candidate is prepared, not just the one that wins."""
        rawimage.set_instance_namespace(name)
        self.addCleanup(shutil.rmtree,
                        os.path.join(tempfile.gettempdir(), name),
                        ignore_errors=True)

    def _neighbour(self, base, name):
        """Somebody else's directory, old enough to be reclaimable by age."""
        path = os.path.join(base, name)
        os.makedirs(path)
        with open(os.path.join(path, "theirs.txt"), "w") as fh:
            fh.write("not ours")
        os.utime(path, (1_000_000, 1_000_000))
        return path

    def test_a_sweep_with_no_prefix_is_refused(self):
        # "" is not a narrower sweep than "mpvtk-", it is the widest one
        # there is: everything startswith it, so every neighbour falls
        # through to the pid and age rules meant for our own leftovers.
        theirs = self._neighbour(self.base, "fontconfig")
        with self.assertRaises(ValueError):
            rawimage.sweep_stale(self.base, "")
        self.assertTrue(os.path.exists(theirs))

    def test_inside_a_namespace_no_prefix_is_fine(self):
        # There it is genuinely ignored -- the directory itself is the scope.
        rawimage.set_instance_namespace("jms-scope-ns0")
        home = os.path.join(self.base, "jms-scope-ns0")
        os.makedirs(home)
        stale = self._neighbour(home, "jms-thumbs-1-abc")
        rawimage.sweep_stale(home, "")
        self.assertFalse(os.path.exists(stale))

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_a_symlinked_namespace_is_not_followed(self):
        # /tmp and /dev/shm are world-writable, and the namespace name is
        # derived from a config path that is not hard to guess -- so the
        # directory has to be shown to be ours before anything inside it is
        # swept, or a symlink planted there aims the sweep somewhere else.
        elsewhere = tempfile.mkdtemp(prefix="jms-elsewhere-")
        self.addCleanup(shutil.rmtree, elsewhere, ignore_errors=True)
        theirs = self._neighbour(elsewhere, "Photos")
        self._namespace("jms-scope-ns1")
        os.symlink(elsewhere, os.path.join(self.base, "jms-scope-ns1"))

        path = rawimage.cache_dir(prefix="jms-x-", min_free=0)
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)

        self.assertTrue(os.path.exists(theirs),
                        "swept a directory the namespace merely pointed at")
        self.assertFalse(path.startswith(self.base + os.sep),
                         "cached into a base whose namespace is a symlink")

    @unittest.skipIf(os.name == "nt", "POSIX ownership")
    def test_a_namespace_someone_else_owns_loses_the_base(self):
        # The same directory as a real one made by another user: still not
        # ours to empty, and not ours to write in either.
        from unittest import mock

        self._namespace("jms-scope-ns2")
        home = os.path.join(self.base, "jms-scope-ns2")
        os.makedirs(home)
        theirs = self._neighbour(home, "jms-thumbs-1-abc")
        somebody_else = os.getuid() + 1        # not through the patch below
        with mock.patch.object(os, "getuid", lambda: somebody_else):
            path = rawimage.cache_dir(prefix="jms-x-", min_free=0)
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)

        self.assertTrue(os.path.exists(theirs))
        self.assertFalse(path.startswith(self.base + os.sep))

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_the_namespace_directory_is_private(self):
        # Nobody else can plant anything in a directory swept this broadly.
        self._namespace("jms-scope-ns3")
        path = rawimage.cache_dir(prefix="jms-x-", min_free=0)
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        mode = os.stat(os.path.dirname(path)).st_mode
        self.assertEqual(stat.S_IMODE(mode) & 0o077, 0,
                         "the namespace directory is group/world accessible")
