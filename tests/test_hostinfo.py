"""What kind of install this is, asked of the host.

One probe answers two questions -- whether an update notice can say anything
useful, and whether the gamepad should be on -- so a mistake here reaches
both. Everything is driven through the module's own path constants rather
than by faking `os.path.exists` wholesale, because the thing most likely to
be wrong is *which file it reads*: inside a Flatpak `/etc/os-release`
describes the runtime and answering from it would call every Steam Deck a
Freedesktop SDK.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.argv = ["test"]

from jellyfin_mpv_shim import hostinfo  # noqa: E402

STEAMOS = ('NAME="SteamOS"\nID=steamos\nID_LIKE=arch\n'
           'PRETTY_NAME="SteamOS Holo"\n')
DEBIAN = ('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nNAME="Debian GNU/Linux"'
          '\nID=debian\nHOME_URL="https://www.debian.org/"\n')


class Probe(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(self._clean)
        self.marker = os.path.join(self.dir, "flatpak-info")
        self.host = os.path.join(self.dir, "host-os-release")
        self.etc = os.path.join(self.dir, "etc-os-release")
        for name, value in (("FLATPAK_MARKER", self.marker),
                            ("HOST_OS_RELEASE", self.host)):
            patch = mock.patch.object(hostinfo, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def _clean(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, path, text):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _in_flatpak(self):
        self._write(self.marker, "[Application]\nname=com.example\n")

    def test_no_marker_is_not_a_flatpak(self):
        self.assertFalse(hostinfo.is_flatpak())
        self.assertFalse(hostinfo.flatpak_managed())

    def test_a_flatpak_is_managed_unless_it_is_marked(self):
        self._in_flatpak()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(hostinfo.UPDATE_MARKER_ENV, None)
            self.assertTrue(hostinfo.flatpak_managed())
        with mock.patch.dict(os.environ,
                             {hostinfo.UPDATE_MARKER_ENV: "1"}):
            self.assertFalse(
                hostinfo.flatpak_managed(),
                "a build we handed somebody has no remote to update from, "
                "so it must keep its link to the releases page")

    def test_the_marker_alone_is_not_enough(self):
        """Outside a Flatpak the variable means nothing: it selects *which*
        advice is right for a sandbox, not whether there is one."""
        with mock.patch.dict(os.environ,
                             {hostinfo.UPDATE_MARKER_ENV: "1"}):
            self.assertFalse(hostinfo.flatpak_managed())

    def test_inside_a_flatpak_the_host_file_is_the_one_read(self):
        """The whole reason this module exists. `/etc/os-release` in a
        sandbox is the runtime's, so a Steam Deck answering from it is a
        Freedesktop SDK and the gamepad never comes on."""
        self._in_flatpak()
        self._write(self.host, STEAMOS)
        self.assertTrue(hostinfo.is_steamos())

    def test_outside_a_flatpak_etc_is(self):
        with mock.patch("builtins.open", mock.mock_open(read_data=STEAMOS)) \
                as opened:
            self.assertTrue(hostinfo.is_steamos())
        self.assertEqual(opened.call_args[0][0], "/etc/os-release")

    def test_a_machine_that_will_not_say_gets_the_ordinary_defaults(self):
        """No file, or an unreadable one: empty, never an exception. This
        runs during config load, before anything is on screen."""
        self._in_flatpak()
        self.assertEqual(hostinfo.os_release(), "")
        self.assertFalse(hostinfo.is_steamos())

    def test_an_ordinary_distribution_is_not_steamos(self):
        self._in_flatpak()
        self._write(self.host, DEBIAN)
        self.assertFalse(hostinfo.is_steamos())

    def test_a_url_that_mentions_steamos_is_not_steamos(self):
        """The negative control for matching on the NAME line rather than
        anywhere in the file: a Steam Deck's own os-release has "steamos" in
        two URLs, and so does a distribution that merely links to it."""
        self._in_flatpak()
        self._write(self.host, DEBIAN + 'BUG_REPORT_URL="https://steamos.example/"\n')
        self.assertFalse(hostinfo.is_steamos())

    def test_an_unquoted_name_still_counts(self):
        self._in_flatpak()
        self._write(self.host, "NAME=SteamOS\nID=holo\n")
        self.assertTrue(hostinfo.is_steamos())


class MsixProbe(unittest.TestCase):
    """Package identity, asked of a stand-in kernel32 answering with the
    codes Windows gives: asked with a zero-length buffer, a packaged process
    gets ERROR_INSUFFICIENT_BUFFER and the length, an unpackaged one
    APPMODEL_ERROR_NO_PACKAGE."""

    def _probe(self, kernel32, platform="win32"):
        import ctypes

        windll = mock.Mock(kernel32=kernel32)
        with mock.patch.object(sys, "platform", platform), \
                mock.patch.object(ctypes, "windll", windll, create=True):
            return hostinfo.package_family_name(), hostinfo.is_msix()

    def _packaged(self, family):
        def get(length_ref, buf):
            if buf is None:
                length_ref._obj.value = len(family) + 1
                return 122
            buf.value = family
            return 0
        return mock.Mock(GetCurrentPackageFamilyName=mock.Mock(side_effect=get))

    def _answering(self, rc):
        return mock.Mock(
            GetCurrentPackageFamilyName=mock.Mock(return_value=rc))

    def test_a_packaged_process_reports_its_family(self):
        self.assertEqual(self._probe(self._packaged("Jms_abc123")),
                         ("Jms_abc123", True))

    def test_an_unpackaged_process_is_not(self):
        self.assertEqual(self._probe(self._answering(15700)), (None, False))

    def test_windows_without_the_function_is_not(self):
        """Windows 7: kernel32 has no GetCurrentPackageFamilyName."""
        self.assertEqual(self._probe(mock.Mock(spec=[])), (None, False))

    def test_only_windows_is_asked(self):
        kernel32 = self._packaged("Jms_abc123")
        self.assertEqual(self._probe(kernel32, platform="linux"),
                         (None, False))
        kernel32.GetCurrentPackageFamilyName.assert_not_called()

    def test_either_updater_manages_updates(self):
        for flatpak, msix in ((True, False), (False, True), (False, False)):
            with mock.patch.object(hostinfo, "flatpak_managed",
                                   lambda: flatpak), \
                    mock.patch.object(hostinfo, "is_msix", lambda: msix):
                self.assertEqual(hostinfo.updates_managed(), flatpak or msix)


class OutsidePath(unittest.TestCase):
    """What another program sees of a path under a packaged app's AppData.
    Real directories stand in for %APPDATA% and %LOCALAPPDATA%, because the
    rule turns on which copy exists."""

    FAMILY = "Jms_abc123"

    def setUp(self):
        import shutil

        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.roaming = os.path.join(self.root, "Roaming")
        self.local = os.path.join(self.root, "Local")
        self.cache = os.path.join(self.local, "Packages", self.FAMILY,
                                  "LocalCache")
        env = mock.patch.dict(os.environ, {"APPDATA": self.roaming,
                                           "LOCALAPPDATA": self.local})
        env.start()
        self.addCleanup(env.stop)
        self.family = self.FAMILY
        fam = mock.patch.object(hostinfo, "package_family_name",
                                lambda: self.family)
        fam.start()
        self.addCleanup(fam.stop)

    def _mkdir(self, *parts):
        path = os.path.join(*parts)
        os.makedirs(path, exist_ok=True)
        return path

    def test_a_fresh_install_config_is_found_in_the_package_cache(self):
        redirected = self._mkdir(self.cache, "Roaming", "jellyfin-mpv-shim")
        asked = os.path.join(self.roaming, "jellyfin-mpv-shim")
        self.assertEqual(hostinfo.outside_path(asked), redirected)

    def test_the_local_side_maps_to_its_own_folder(self):
        redirected = self._mkdir(self.cache, "Local", "jellyfin-mpv-shim",
                                 "cache")
        asked = os.path.join(self.local, "jellyfin-mpv-shim", "cache")
        self.assertEqual(hostinfo.outside_path(asked), redirected)

    def test_a_config_from_before_the_install_stays_where_it_is(self):
        """Measured on the VM: pre-existing folders are used in place, so
        there is no redirected copy, and the real path is the answer."""
        asked = self._mkdir(self.roaming, "jellyfin-mpv-shim")
        self.assertEqual(hostinfo.outside_path(asked), asked)

    def test_paths_elsewhere_are_untouched(self):
        self._mkdir(self.cache, "Roaming", "x")
        elsewhere = os.path.join(self.root, "Videos", "x")
        self.assertEqual(hostinfo.outside_path(elsewhere), elsewhere)
        self.assertEqual(hostinfo.outside_path(self.roaming), self.roaming)

    def test_an_unpackaged_process_is_untouched(self):
        self._mkdir(self.cache, "Roaming", "jellyfin-mpv-shim")
        self.family = None
        asked = os.path.join(self.roaming, "jellyfin-mpv-shim")
        self.assertEqual(hostinfo.outside_path(asked), asked)

if __name__ == "__main__":
    unittest.main()
