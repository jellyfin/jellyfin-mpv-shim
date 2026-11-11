"""The installers and packages agree with each other and with the app's identity.

Plain file reads: nothing here builds anything or opens a window. What these cannot show is that a package
installs and runs on its platform; docs/native-verification.md is the record of that.
"""

if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import os
import re
import stat
import sys
import unittest
import xml.dom.minidom

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim import constants  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class LinuxPackageTest(unittest.TestCase):
    def test_the_launcher_exists_is_executable_and_is_what_the_desktop_entry_runs(self):
        path = os.path.join(ROOT, "linux", "jellyhome-player")
        self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR)
        first = read("linux", "jellyhome-player").splitlines()[0]
        # One argument after the interpreter: Linux does not split "-s -E".
        self.assertEqual(first, "#!/usr/bin/python3 -sE")
        desktop = read("jellyfin_mpv_shim", "integration", constants.DESKTOP_ID + ".desktop")
        self.assertIn("Exec=jellyhome-player", desktop)

    def test_the_launcher_exports_its_path_for_autostart(self):
        self.assertIn("JELLYHOME_LAUNCHER", read("linux", "jellyhome-player"))
        self.assertIn("JELLYHOME_LAUNCHER", read("jellyfin_mpv_shim", "autostart.py"))

    def test_the_build_script_names_the_same_app_id_and_dependencies(self):
        script = read("linux", "build-deb.sh")
        self.assertIn('appid="%s"' % constants.DESKTOP_ID, script)
        for needle in ("python3 (>= 3.13)", "python3 (<< 3.14)", "libmpv2", "libfribidi0"):
            self.assertIn(needle, script)
        # No autostart entry and no files in a user's home are ever installed.
        self.assertNotIn("/etc/xdg/autostart", script)
        self.assertNotRegex(script, r"/home/|~/\.config/jellyhome-player/")

    def test_every_locked_requirement_is_hash_pinned(self):
        lines = [l for l in read("linux", "requirements-linux-cp313-amd64.lock").splitlines()
                 if l.strip() and not l.startswith("#")]
        self.assertGreaterEqual(len(lines), 10)
        for line in lines:
            with self.subTest(line=line.split()[0]):
                self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[0-9][^ ]* --hash=sha256:[0-9a-f]{64}$")

    def test_the_lock_covers_what_the_package_requires(self):
        lock = read("linux", "requirements-linux-cp313-amd64.lock").lower().replace("_", "-")
        for req in ("python-mpv", "python-mpv-jsonipc", "jellyfin-apiclient-python", "requests", "pillow", "certifi"):
            self.assertRegex(lock, r"(?m)^%s==" % re.escape(req))
        self.assertRegex(lock, r"(?m)^pillow==12\.3\.0 ", "Pillow is pinned to the tested version")
        m = re.search(r"(?m)^certifi==(\d+)\.(\d+)\.(\d+)", lock)
        self.assertGreaterEqual((int(m[1]), int(m[2]), int(m[3])), (2026, 10, 19))   # pyproject's floor

    def test_the_bundled_libraries_are_all_named_in_the_notices(self):
        notices = read("THIRD_PARTY_NOTICES.md").lower()
        for line in read("linux", "requirements-linux-cp313-amd64.lock").splitlines():
            if line.strip() and not line.startswith("#"):
                name = line.split("==")[0].lower()
                self.assertIn(name, notices, "%s is bundled but not in THIRD_PARTY_NOTICES.md" % name)

    def test_the_metainfo_names_the_installed_binary_and_has_one_launchable(self):
        text = read("jellyfin_mpv_shim", "integration", constants.DESKTOP_ID + ".appdata.xml")
        dom = xml.dom.minidom.parseString(text)
        self.assertEqual([n.firstChild.data for n in dom.getElementsByTagName("binary")],
                         ["jellyhome-player"])
        self.assertEqual(len(dom.getElementsByTagName("launchable")), 1)
        self.assertNotIn("​", text)

    def test_the_shader_pack_is_pinned_by_tag_and_content_not_taken_from_latest(self):
        script = read("linux", "build-deb.sh")
        self.assertRegex(script, r'(?m)^PINNED_SHADER_PACK_TAG="v\d+\.\d+\.\d+"$')
        self.assertRegex(script, r'(?m)^PINNED_SHADER_PACK_TREE_SHA256="[0-9a-f]{64}"$')
        self.assertNotIn("releases/latest", script)
        self.assertIn("does not match its pinned hash", script)
        # A different tag has to bring its own hash: the pin cannot be bypassed by the tag alone.
        self.assertIn("needs SHADER_PACK_TREE_SHA256", script)

    def test_ci_pins_the_build_tools_and_tests_the_dependencies_alone(self):
        ci = read(".github", "workflows", "main.yml")
        job = ci[ci.index("\n  build-deb:"):]
        self.assertRegex(job, r"pip install pip==[\d.]+ setuptools==[\d.]+ wheel==[\d.]+")
        self.assertNotIn("--upgrade pip", job)
        self.assertNotIn("SHADER_PACK_TAG=", job, "the pin lives in the script")
        self.assertIn("--no-install-recommends", job, "Depends alone must be enough to start")

    def test_the_libmpv_floor_is_the_apps_not_the_newest_features(self):
        script = read("linux", "build-deb.sh")
        self.assertIn("libmpv2 (>= 0.38)", script)
        self.assertNotRegex(script, r"libmpv2 \(>= 0\.4")

    def test_a_text_font_is_recommended_and_the_maintainer_is_overridable(self):
        script = read("linux", "build-deb.sh")
        self.assertRegex(script, r"Recommends:[^\n]*fonts-dejavu-core \| fonts-noto-core")
        self.assertIn("Maintainer: $maintainer", script)
        self.assertIn("DEB_MAINTAINER", script)

    def test_the_readme_the_lock_file_points_at_exists_and_covers_the_knobs(self):
        self.assertIn("linux/README.md", read("linux", "requirements-linux-cp313-amd64.lock"))
        readme = read("linux", "README.md")
        for needle in ("DEB_MAINTAINER", "SHADER_PACK_TAG", "SHADER_PACK_TREE_SHA256", "SOURCE_DATE_EPOCH",
                       "Updating the lock", "libmpv2 (>= 0.38)"):
            self.assertIn(needle, readme)

    def test_the_docs_do_not_say_hiding_the_window_quits_the_app(self):
        # With libmpv 0.40 it quits the mpv instance; the app itself keeps running as a cast target.
        self.assertNotIn("quits the player instead", read("docs", "installation.md"))


class CiTest(unittest.TestCase):
    def test_the_workflow_builds_validates_and_never_publishes(self):
        ci = read(".github", "workflows", "main.yml")
        for job in ("build-deb:", "validate-packaging:", "build-flatpak:", "build-macos:", "build-win64:"):
            self.assertIn("\n  " + job, ci)
        self.assertRegex(ci, r"(?m)^permissions:\n  contents: read")
        self.assertNotRegex(ci, r"(?i)gh release|softprops/action-gh-release|upload-release|\$\{\{\s*secrets\.")


if __name__ == "__main__":
    unittest.main()
