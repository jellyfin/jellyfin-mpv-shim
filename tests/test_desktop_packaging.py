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
UPSTREAM_BUNDLE_ID = "com.github.iwalton3.jellyfin-mpv-shim"


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


MAC_BUILD = os.path.join("macos", "build.sh")


def mac_add_data():
    """(source, destination) of every --add-data in macos/build.sh."""
    return re.findall(r'--add-data "([^":]+):([^"]+)"', read(MAC_BUILD))


def mac_ci_step(name):
    ci = read(".github", "workflows", "main.yml")
    m = re.search(r"(?ms)^    - name: %s\n(.*?)(?=^    - name: |\Z)" % re.escape(name), ci)
    assert m, name
    return m.group(1)


class MacOSBundleTest(unittest.TestCase):
    def setUp(self):
        self.build = read(MAC_BUILD)

    # Identity

    def test_the_bundle_is_the_forks_and_not_upstreams(self):
        self.assertIn('app="%s"' % constants.USER_APP_NAME, self.build)
        self.assertIn("--osx-bundle-identifier " + constants.DESKTOP_ID, self.build)
        self.assertNotIn(UPSTREAM_BUNDLE_ID, self.build)
        self.assertNotIn('app="Jellyfin MPV Shim"', self.build)
        self.assertNotIn("Jellyfin MPV Shim.app", self.build)
        self.assertNotIn("Jellyfin MPV Shim.app", read("macos", "README.md"))

    def test_the_names_shown_to_the_user_are_the_apps(self):
        self.assertIn('plutil -replace CFBundleDisplayName -string "$app"', self.build)
        # The only place the upstream name may appear in the script is the attribution in the copyright line.
        rest = self.build.replace(
            "A fork of Jellyfin MPV Shim; see LICENSE.md and THIRD_PARTY_NOTICES.md.", ""
        )
        self.assertNotIn("Jellyfin MPV Shim", rest)
        self.assertIn("NSHumanReadableCopyright", self.build)

    def test_the_plist_gets_the_version_and_the_real_minimum_system(self):
        self.assertIn("grep '^CLIENT_VERSION' jellyfin_mpv_shim/constants.py", self.build)
        for key in ("CFBundleShortVersionString", "CFBundleVersion"):
            self.assertIn('plutil -replace %s -string "$version"' % key, self.build)
        self.assertIn('plutil -replace LSMinimumSystemVersion -string "$floor"', self.build)
        self.assertIn("plutil -replace LSUIElement -bool true", self.build)

    # Build inputs

    def test_the_notices_are_inside_the_app(self):
        sources = [src for src, _dest in mac_add_data()]
        self.assertIn("THIRD_PARTY_NOTICES.md", sources)
        self.assertIn("LICENSE.md", sources)

    def test_files_the_build_reads_exist(self):
        for src, _dest in mac_add_data():
            if os.path.basename(src) == "default_shader_pack":
                continue  # fetched by gen_pkg.sh at build time
            self.assertTrue(os.path.exists(os.path.join(ROOT, src)), src)
        for path in (
            "jellyfin.icns",
            os.path.join("macos", "jellyfin.icon"),
            os.path.join("macos", "pyi_rth_macos.py"),
            os.path.join("macos", "dmg_settings.py"),
        ):
            self.assertTrue(os.path.exists(os.path.join(ROOT, path)), path)
            self.assertIn(path.replace(os.sep, "/"), self.build)

    def test_mpv_is_a_helper_bundle_the_app_finds_through_resources(self):
        self.assertIn('cp -R macos/mpv.app "$contents/Helpers/mpv.app"', self.build)
        self.assertIn('ln -s ../Helpers/mpv.app/Contents/MacOS/mpv "$contents/Resources/mpv"', self.build)

    def test_the_build_is_one_cpu_per_dmg(self):
        self.assertIn("never universal", self.build)
        self.assertNotIn("universal2", self.build)
        self.assertNotIn("--target-arch", self.build)

    # Output

    def test_the_dmg_name_is_the_one_artifacts_sh_collects(self):
        self.assertIn("dist/jellyhome-player_version.dmg", self.build)
        self.assertIn("dist/jellyhome-player_version.dmg", read("artifacts.sh"))
        # The volume and the file inside it are named after the app.
        self.assertRegex(self.build, r'"\$app" dist/jellyhome-player_version\.dmg')
        self.assertIn("macOS_${2}.dmg", read("artifacts.sh"))

    def test_the_signature_is_ad_hoc_and_verified(self):
        self.assertIn('codesign --force --deep --sign - "$bundle"', self.build)
        self.assertIn('codesign --verify --deep --strict "$bundle"', self.build)
        self.assertNotRegex(self.build, r"(?i)notarytool|--options runtime|Developer ID")

    # CI

    def test_ci_checks_what_it_built(self):
        step = mac_ci_step("Verify the bundle")
        self.assertIn('lipo -archs "$app/Contents/MacOS/JellyHome Player"', step)
        self.assertIn('lipo -archs "$app/Contents/Helpers/mpv.app/Contents/MacOS/mpv"', step)
        self.assertEqual(step.count('= "${{ matrix.arch }}"'), 2)
        self.assertIn('= "%s"' % constants.DESKTOP_ID, step)
        self.assertIn("CFBundleIdentifier", step)
        self.assertIn('codesign --verify --deep --strict "$app"', step)
        self.assertIn("--version", step)
        self.assertIn('app="dist/%s.app"' % constants.USER_APP_NAME, step)
        ci = read(".github", "workflows", "main.yml")
        self.assertLess(ci.index("- name: Main Build\n      run: |\n        ./macos/build.sh"), ci.index("- name: Verify the bundle"))
        self.assertLess(ci.index("- name: Verify the bundle"), ci.index("./artifacts.sh macos"))

    def test_ci_builds_one_dmg_per_cpu_with_the_documented_mpv(self):
        ci = read(".github", "workflows", "main.yml")
        job = ci[ci.index("\n  build-macos:") + 1 :]
        job = re.split(r"(?m)^  [\w-]+:$", job, maxsplit=2)[1]
        self.assertIn("arch: arm64", job)
        self.assertIn("arch: x86_64", job)
        tag = re.search(r"tag: '(v[\d.]+)'", job).group(1)
        self.assertIn(tag, read("macos", "README.md"))
        self.assertIn(tag, read("THIRD_PARTY_NOTICES.md"))
        self.assertIn("MACOSX_DEPLOYMENT_TARGET", job)

    def test_the_docs_describe_this_bundle(self):
        for doc in (("docs", "installation.md"), ("macos", "README.md")):
            text = read(*doc)
            self.assertIn("JellyHome Player.app", text, doc)
            self.assertNotIn(UPSTREAM_BUNDLE_ID, text, doc)
        self.assertIn(constants.DESKTOP_ID, read("docs", "installation.md"))
        self.assertIn("macOS 15", read("docs", "installation.md"))
        self.assertIn("jellyhome-player_<version>_macOS_<arch>.dmg", read("docs", "installation.md"))


WIN_SCRIPTS = ("build-win.bat", "build-win-arm64.bat", "build-win-dbg.bat")
ISS = "JellyHome Player.iss"
# Fetched by gen_pkg.sh at build time (not tracked), so it cannot be checked for existence in a checkout.
FETCHED_AT_BUILD = ("default_shader_pack",)


def iss_define(name):
    m = re.search(r'(?m)^#define %s "([^"]*)"' % name, read(ISS))
    assert m, name
    return m.group(1)


def iss_sections():
    """{"Setup": [lines], "Files": [...]} with comments and blank lines dropped; keeps #ifdef lines in place."""
    out, cur = {}, None
    for line in read(ISS).splitlines():
        line = line.strip()
        m = re.match(r"\[(\w+)\]$", line)
        if m:
            cur = out.setdefault(m.group(1), [])
        elif cur is not None and line and not line.startswith(";"):
            cur.append(line)
    return out


def pyinstaller_args(script):
    """(name, add-data pairs) of the one pyinstaller command in a build script."""
    text = read(script)
    name = re.search(r"--name (\S+)", text).group(1)
    data = re.findall(r'--add-data "([^";]+);([^"]+)"', text)
    return name, data


class WindowsInstallerTest(unittest.TestCase):
    # Identity

    def test_the_installer_is_named_and_versioned_like_the_app(self):
        self.assertEqual(iss_define("MyAppName"), constants.USER_APP_NAME)
        self.assertEqual(iss_define("MyAppVersion"), constants.CLIENT_VERSION)
        setup = iss_sections()["Setup"]
        self.assertIn("AppName={#MyAppName}", setup)
        self.assertIn("AppVersion={#MyAppVersion}", setup)
        self.assertNotIn("Jellyfin MPV Shim", read(ISS))

    def test_the_installer_has_its_own_app_id(self):
        ids = re.findall(r"(?m)^AppId=\{\{([0-9A-F-]{36})\}", read(ISS))
        self.assertEqual(len(ids), 1)
        self.assertRegex(ids[0], r"^[0-9A-F]{8}-([0-9A-F]{4}-){3}[0-9A-F]{12}$")

    def test_the_installer_executable_is_what_every_pyinstaller_script_builds(self):
        exe = iss_define("MyAppExeName")
        for script in WIN_SCRIPTS:
            name, _ = pyinstaller_args(script)
            self.assertEqual(name + ".exe", exe, script)
        # ...and is the payload directory the [Files] section copies.
        self.assertIn('Source: "dist\\%s\\*"' % exe[:-4], read(ISS))

    def test_shortcuts_run_and_close_the_right_executable(self):
        sections = iss_sections()
        icons = sections["Icons"]
        self.assertEqual(len(icons), 2)
        for line in icons:
            self.assertIn('Filename: "{app}\\{#MyAppExeName}"', line)
        self.assertTrue(icons[0].startswith('Name: "{autoprograms}\\{#MyAppName}"'))
        self.assertTrue(icons[1].startswith('Name: "{autodesktop}\\{#MyAppName}"'))
        self.assertIn("Tasks: desktopicon", icons[1])
        self.assertIn("UninstallDisplayIcon={app}\\{#MyAppExeName}", sections["Setup"])
        self.assertIn("CloseApplicationsFilter={#MyAppExeName},mpv-2.dll", sections["Setup"])
        self.assertTrue(sections["Run"][0].startswith('Filename: "{app}\\{#MyAppExeName}"'))

    # Install, upgrade and uninstall

    def test_install_stays_per_user_and_upgrades_in_place(self):
        setup = iss_sections()["Setup"]
        for line in (
            "PrivilegesRequired=lowest",
            "PrivilegesRequiredOverridesAllowed=dialog",
            "DefaultDirName={autopf}\\{#MyAppName}",
            "CloseApplications=yes",
            "RestartApplications=no",
        ):
            self.assertIn(line, setup)

    def test_the_installer_filename_is_the_one_artifacts_sh_collects(self):
        self.assertIn("OutputBaseFilename=jellyhome-player_version_installer", iss_sections()["Setup"])
        self.assertIn("dist/jellyhome-player_version_installer.exe", read("artifacts.sh"))

    def test_uninstall_does_not_delete_user_data(self):
        sections = iss_sections()
        for section in ("UninstallDelete", "InstallDelete", "Code"):
            self.assertNotIn(section, sections)
        # Directives only (comments explain where the settings live and are dropped).
        directives = "\n".join(l for lines in sections.values() for l in lines)
        self.assertNotRegex(directives, r"(?i)\{userappdata\}|\{localappdata\}|deletekey|dirifempty")

    def test_start_with_computer_is_the_apps_own_opt_in_and_uninstall_removes_it(self):
        from jellyfin_mpv_shim import autostart

        entries = iss_sections()["Registry"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertIn("Root: HKCU;", entry)
        self.assertIn('Subkey: "%s"' % autostart.RUN_KEY, entry)
        self.assertIn('ValueName: "{#MyAppName}"', entry)
        self.assertEqual(iss_define("MyAppName"), constants.USER_APP_NAME)
        flags = re.search(r"Flags: ([\w ]+)$", entry).group(1).split()
        self.assertEqual(sorted(flags), ["dontcreatekey", "uninsdeletevalue"])
        # With no ValueType or ValueData the installer creates nothing: autostart stays off until the user turns it on.
        self.assertNotRegex(entry, r"ValueType|ValueData")

    # Architecture

    def test_arm64_restrictions_apply_to_the_arm64_build_only(self):
        iss = read(ISS)
        block = re.search(r"(?s)#ifdef Arm64\n(.*?)#endif", iss)
        self.assertIsNotNone(block)
        self.assertEqual(
            block.group(1).split(), ["ArchitecturesAllowed=arm64", "ArchitecturesInstallIn64BitMode=arm64"]
        )
        self.assertEqual(iss.count("ArchitecturesAllowed"), 1)
        self.assertEqual(iss.count("ArchitecturesInstallIn64BitMode"), 1)
        self.assertIn("/DArm64", read("build-win-arm64.bat"))
        for script in ("build-win.bat", "build-win-dbg.bat"):
            self.assertNotIn("Arm64", read(script))

    # Files the installer and the builds depend on

    def test_files_the_installer_reads_exist(self):
        setup = iss_sections()["Setup"]
        for key in ("LicenseFile", "SetupIconFile"):
            path = [l.split("=", 1)[1] for l in setup if l.startswith(key + "=")]
            self.assertEqual(len(path), 1, key)
            self.assertTrue(os.path.isfile(os.path.join(ROOT, path[0])), path[0])
        sources = [re.match(r'Source: "([^"]+)"', l).group(1) for l in iss_sections()["Files"]]
        self.assertIn("THIRD_PARTY_NOTICES.md", sources)
        self.assertIn("LICENSE.md", sources)
        for src in sources:
            if src.startswith("dist\\"):
                continue  # PyInstaller output
            self.assertTrue(os.path.isfile(os.path.join(ROOT, src)), src)

    def test_pyinstaller_resources_exist_and_the_scripts_agree(self):
        reference = None
        for script in WIN_SCRIPTS:
            _, data = pyinstaller_args(script)
            self.assertTrue(data, script)
            for src, _dest in data:
                rel = src.replace("\\", "/")
                if os.path.basename(rel) in FETCHED_AT_BUILD:
                    continue
                self.assertTrue(os.path.exists(os.path.join(ROOT, rel)), "%s: %s" % (script, rel))
            self.assertEqual(len(data), len(set(data)), script)
            reference = reference or sorted(data)
            self.assertEqual(sorted(data), reference, script)
        icon = re.search(r"--icon (\S+)", read("build-win.bat")).group(1)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, icon)))
        self.assertIn("SetupIconFile=" + icon, iss_sections()["Setup"])


class CiTest(unittest.TestCase):
    def test_the_workflow_builds_validates_and_never_publishes(self):
        ci = read(".github", "workflows", "main.yml")
        for job in ("build-deb:", "validate-packaging:", "build-flatpak:", "build-macos:", "build-win64:"):
            self.assertIn("\n  " + job, ci)
        self.assertRegex(ci, r"(?m)^permissions:\n  contents: read")
        self.assertNotRegex(ci, r"(?i)gh release|softprops/action-gh-release|upload-release|\$\{\{\s*secrets\.")


if __name__ == "__main__":
    unittest.main()
