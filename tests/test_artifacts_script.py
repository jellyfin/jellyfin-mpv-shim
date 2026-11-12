"""artifacts.sh collects (or, for the .deb, checks) the files a CI job built, and refuses anything it does not know.

Each test runs a copy of the real script in a temporary directory with a fake constants.py, so nothing in the
repository's own dist/ or publish/ is read or written.
"""

if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.argv = [sys.argv[0]]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ArtifactsScriptTest(unittest.TestCase):
    version = "3.3.0"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jh-artifacts-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copy(os.path.join(ROOT, "artifacts.sh"), self.tmp)
        self.write("jellyfin_mpv_shim/constants.py", 'CLIENT_VERSION = "%s"\n' % self.version)

    def write(self, rel, text="x"):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def run_script(self, *args):
        return subprocess.run(
            ["bash", "artifacts.sh", *args],
            cwd=self.tmp,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def exists(self, rel):
        return os.path.exists(os.path.join(self.tmp, rel))

    def deb(self, version=None):
        name = "jellyhome-player_%s-1_amd64.deb" % (version or self.version)
        self.write("publish/Linux/" + name)
        self.write("publish/Linux/" + name + ".sha256")
        return name

    # .deb

    def test_deb_present_succeeds_and_does_not_touch_it(self):
        name = self.deb()
        before = os.stat(os.path.join(self.tmp, "publish/Linux", name)).st_mtime_ns
        r = self.run_script("deb", "amd64")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.stat(os.path.join(self.tmp, "publish/Linux", name)).st_mtime_ns, before)

    def test_deb_missing_fails_and_names_the_file(self):
        r = self.run_script("deb", "amd64")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("jellyhome-player_3.3.0-1_amd64.deb", r.stderr)

    def test_deb_without_its_checksum_fails(self):
        name = self.deb()
        os.remove(os.path.join(self.tmp, "publish/Linux", name + ".sha256"))
        r = self.run_script("deb", "amd64")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(".sha256", r.stderr)

    def test_deb_of_another_version_is_not_accepted(self):
        self.deb("3.2.0")
        r = self.run_script("deb", "amd64")
        self.assertNotEqual(r.returncode, 0)

    def test_deb_prerelease_uses_the_debian_tilde_name(self):
        self.write("jellyfin_mpv_shim/constants.py", 'CLIENT_VERSION = "3.3.0rc1"\n')
        self.assertNotEqual(self.run_script("deb", "amd64").returncode, 0)
        self.write("publish/Linux/jellyhome-player_3.3.0~rc1-1_amd64.deb")
        self.write("publish/Linux/jellyhome-player_3.3.0~rc1-1_amd64.deb.sha256")
        # The script's rule is "pre" -> "~pre" (as in linux/build-deb.sh), so "rc1" is left alone.
        self.assertNotEqual(self.run_script("deb", "amd64").returncode, 0)
        self.write("jellyfin_mpv_shim/constants.py", 'CLIENT_VERSION = "3.3.0pre1"\n')
        self.write("publish/Linux/jellyhome-player_3.3.0~pre1-1_amd64.deb")
        self.write("publish/Linux/jellyhome-player_3.3.0~pre1-1_amd64.deb.sha256")
        self.assertEqual(self.run_script("deb", "amd64").returncode, 0)

    def test_deb_unsupported_architecture_fails_even_if_a_package_exists(self):
        self.deb()
        for arch in ("arm64", "", "amd64 "):
            r = self.run_script("deb", arch)
            self.assertNotEqual(r.returncode, 0, arch)
            self.assertIn("unsupported deb architecture", r.stderr)

    def test_deb_without_an_architecture_fails(self):
        self.deb()
        self.assertNotEqual(self.run_script("deb").returncode, 0)

    # unknown input

    def test_unknown_or_missing_type_fails(self):
        for args in (("rpm", "amd64"), ("DEB", "amd64"), ()):
            r = self.run_script(*args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("unknown artifact type", r.stderr)

    # Windows and macOS keep working

    def test_windows_installers_are_collected_under_their_names(self):
        for kind, suffix in (("standard", ""), ("legacy64", "_LEGACY64"), ("arm64", "_ARM64")):
            self.write("dist/jellyhome-player_version_installer.exe", kind)
            r = self.run_script(kind)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = "publish/Installer/jellyhome-player_3.3.0%s_installer.exe" % suffix
            with open(os.path.join(self.tmp, out), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), kind)

    def test_missing_windows_installer_fails(self):
        self.assertNotEqual(self.run_script("standard").returncode, 0)

    def test_macos_dmg_is_collected_under_its_architecture(self):
        self.write("dist/jellyhome-player_version.dmg", "dmg")
        r = self.run_script("macos", "arm64")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.exists("publish/macOS/jellyhome-player_3.3.0_macOS_arm64.dmg"))

    def test_missing_macos_dmg_fails(self):
        self.assertNotEqual(self.run_script("macos", "arm64").returncode, 0)

    def test_flatpak_exports_the_bundle_from_the_repo(self):
        fake = os.path.join(self.tmp, "bin")
        os.makedirs(fake)
        log = os.path.join(self.tmp, "flatpak.args")
        with open(os.path.join(fake, "flatpak"), "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\necho "$@" > "%s"\n' % log)
        os.chmod(os.path.join(fake, "flatpak"), 0o755)
        env = dict(os.environ, PATH=fake + os.pathsep + os.environ["PATH"])
        r = subprocess.run(
            ["bash", "artifacts.sh", "flatpak", "amd64"],
            cwd=self.tmp, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(log, encoding="utf-8") as fh:
            args = fh.read()
        self.assertIn("build-bundle repo publish/Flatpak/jellyhome-player_v3.3.0_amd64.flatpak", args)


if __name__ == "__main__":
    unittest.main()
