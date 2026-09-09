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


if __name__ == "__main__":
    unittest.main()
