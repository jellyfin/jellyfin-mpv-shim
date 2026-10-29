"""Settings and paths that are read before the app is up, where a bad value
used to stop it starting at all (#846-#849)."""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import argparse
import io
import logging
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim import conffile, log_utils  # noqa: E402
from jellyfin_mpv_shim.conf import Settings  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.config import ENUMS  # noqa: E402
from jellyfin_mpv_shim.sync.db import SyncDB  # noqa: E402
from jellyfin_mpv_shim.sync.manager import resolve_root  # noqa: E402


class AppLogLevelTest(unittest.TestCase):
    """mpv_log_level sets the app's own log level too, so every value the
    settings offer or docs/configuration.md lists must configure it."""

    #: In docs/configuration.md but not in the settings dropdown.
    DOCUMENTED_ONLY = ("v", "trace")

    def configure(self, name):
        root = logging.getLogger()
        before = set(root.handlers)
        self.addCleanup(setattr, root, "level", root.level)
        log_utils.configure_log(io.StringIO(), name)
        (handler,) = set(root.handlers) - before
        self.addCleanup(root.removeHandler, handler)
        return handler.level

    def test_every_offered_level_starts_the_app(self):
        for name in list(ENUMS["mpv_log_level"]) + list(self.DOCUMENTED_ONLY):
            with self.subTest(level=name):
                self.configure(name)

    def test_mpv_names_land_in_order(self):
        self.assertEqual(self.configure("noise"), logging.DEBUG)
        self.assertLess(logging.DEBUG, self.configure("v"))
        self.assertLess(self.configure("v"), logging.INFO)
        self.assertLess(self.configure("trace"), logging.DEBUG)

    def test_v_has_a_name_in_the_log(self):
        """Otherwise every mpv `v` line is labelled "Level 15"."""
        self.assertNotEqual("Level 15",
                            logging.getLevelName(log_utils._level("v")))

    def test_a_typo_falls_back_to_info(self):
        self.assertEqual(self.configure("verbose!"), logging.INFO)


class ConfigDirTest(unittest.TestCase):
    def setUp(self):
        self.cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cwd, ignore_errors=True)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.cwd)

    def with_config(self, value):
        p = mock.patch.object(conffile, "get_args",
                              return_value=argparse.Namespace(config=value))
        p.start()
        self.addCleanup(p.stop)

    def test_a_relative_config_opens_the_catalog_read_only(self):
        """#847's crash: the second launch opens an existing catalog
        read-only, through a file URI that a relative path cannot be."""
        self.with_config("cfg")
        root = conffile.get_dir("app", "offline")
        SyncDB(os.path.join(root, "catalog.db")).close()
        SyncDB(os.path.join(root, "catalog.db"), read_only=True).close()
        self.assertTrue(os.path.isabs(conffile.confdir("app")))
        self.assertTrue(os.path.isabs(conffile.cachedir("app")))

    def test_an_unlisted_posix_system_gets_xdg_paths(self):
        with mock.patch.object(conffile.sys, "platform", "freebsd14"), \
                mock.patch.object(conffile.os, "name", "posix"):
            self.assertIs(conffile._for_platform(conffile.confdirs,
                                                 conffile.posix),
                          conffile.posix)
            self.assertIs(conffile._for_platform(conffile.cachedirs,
                                                 conffile.posix_cache),
                          conffile.posix_cache)

    def test_an_unlisted_non_posix_system_still_gets_nothing(self):
        with mock.patch.object(conffile.sys, "platform", "plan9"), \
                mock.patch.object(conffile.os, "name", "nt"):
            self.assertIsNone(conffile._for_platform(conffile.confdirs,
                                                     conffile.posix))


class DownloadFolderTest(unittest.TestCase):
    """A hand-edited relative `sync_path` reached SyncDB as-is: the same
    crash as #847, and a different folder for every launch directory."""

    def setUp(self):
        self.conf = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.conf, ignore_errors=True)
        p = mock.patch("jellyfin_mpv_shim.sync.manager.confdir",
                       return_value=self.conf)
        p.start()
        self.addCleanup(p.stop)

    def test_a_relative_path_is_inside_the_config_directory(self):
        self.assertEqual(resolve_root("dl"), os.path.join(self.conf, "dl"))

    def test_the_default_is_the_offline_folder(self):
        self.assertEqual(resolve_root(None),
                         os.path.join(self.conf, "offline"))

    def test_an_absolute_path_is_kept(self):
        other = os.path.abspath(os.path.join(os.sep, "srv", "dl"))
        self.assertEqual(resolve_root(other), other)

    def test_home_is_expanded(self):
        self.assertEqual(resolve_root("~/dl"),
                         os.path.join(os.path.expanduser("~"), "dl"))


class MissingConfigFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "conf.json")

    def test_a_missing_file_is_created(self):
        Settings().load(self.path)
        self.assertTrue(os.path.isfile(self.path))

    def test_without_create_a_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            Settings().load(self.path, create=False)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0,
                         "needs a directory this user cannot enter")
    def test_a_config_dir_it_cannot_enter_names_the_error(self):
        # os.path.exists answers False for it, so this reaches the open.
        os.chmod(self.tmp, 0)
        self.addCleanup(os.chmod, self.tmp, 0o700)
        with self.assertRaises(PermissionError):
            Settings().load(self.path)


if __name__ == "__main__":
    unittest.main()
