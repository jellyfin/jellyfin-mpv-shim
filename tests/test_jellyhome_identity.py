"""JellyHome Player identity, packaging and first-run.

Windows/Flatpak behaviour is checked as far as a Linux host allows: the
packaging files are parsed and cross-checked rather than built.
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
import re
import sys
import unittest
import xml.dom.minidom


sys.argv = ["test"]

from jellyfin_mpv_shim import constants   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPSTREAM_ISS_GUID = "898D84F4-BAD5-4014-B224-6536BFAE0A31"
UPSTREAM_ID = "com.github.iwalton3.jellyfin-mpv-shim"


def _text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _json(path):
    return json.loads(_text(path))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class IdentityIsDistinctFromUpstreamTest(unittest.TestCase):
    def test_constants_do_not_collide_with_upstream(self):
        self.assertNotEqual(constants.APP_NAME, constants.UPSTREAM_APP_NAME)
        self.assertEqual(constants.UPSTREAM_APP_NAME, "jellyfin-mpv-shim")
        self.assertNotEqual(constants.DESKTOP_ID, UPSTREAM_ID)
        self.assertNotIn("Jellyfin MPV Shim", constants.USER_APP_NAME)

    def test_config_directories_differ(self):
        from jellyfin_mpv_shim import conffile
        self.assertNotEqual(conffile.confdir(constants.APP_NAME),
                            conffile.confdir(constants.UPSTREAM_APP_NAME))

    def test_inno_setup_has_its_own_guid_and_per_user_install(self):
        iss = _read("JellyHome Player.iss")
        self.assertNotIn(UPSTREAM_ISS_GUID, iss)
        m = re.search(r"^AppId=\{\{([0-9A-F-]{36})\}", iss, re.M)
        self.assertIsNotNone(m, "AppId must be a GUID")
        self.assertRegex(iss, r"(?m)^PrivilegesRequired=lowest")
        self.assertRegex(iss, r"(?m)^CloseApplications=yes")  # ask, not force
        self.assertNotIn("CloseApplications=force", iss)
        # Only this app's own files may trigger a close prompt.
        flt = re.search(r"(?m)^CloseApplicationsFilter=(.*)$", iss).group(1)
        self.assertNotIn("*", flt)
        self.assertIn("JellyHomePlayer.exe", iss)
        self.assertNotIn("run.exe", iss)
        # Desktop shortcut optional (unchecked); launch-after-install present.
        self.assertRegex(iss, r'(?m)^Name: "desktopicon".*Flags: unchecked')
        self.assertIn("postinstall", iss)
        self.assertIn('#define MyAppName "%s"' % constants.USER_APP_NAME, iss)

    def test_windows_build_scripts_name_the_exe_the_installer_expects(self):
        for bat in ("build-win.bat", "build-win-arm64.bat", "build-win-dbg.bat"):
            with self.subTest(bat=bat):
                text = _read(bat)
                self.assertIn("--name JellyHomePlayer", text)
                self.assertNotIn("Jellyfin MPV Shim.iss", text)
        self.assertFalse(os.path.exists(os.path.join(ROOT,
                                                     "Jellyfin MPV Shim.iss")))

    def test_flatpak_manifest_desktop_and_appdata_agree(self):
        manifest = json.loads(_read("flatpak",
                                    constants.DESKTOP_ID + ".json"))
        self.assertEqual(manifest["app-id"], constants.DESKTOP_ID)
        self.assertEqual(manifest["command"], "jellyhome-player")
        build = " ".join(c for m in manifest["modules"] if isinstance(m, dict)
                         for c in m.get("build-commands", []))
        for name in (constants.DESKTOP_ID + ".desktop",
                     constants.DESKTOP_ID + ".appdata.xml",
                     constants.DESKTOP_ID + ".png"):
            self.assertIn(name, build)
        self.assertNotIn(UPSTREAM_ID, json.dumps(manifest)
                         .replace('"//', '"'))   # notes may cite upstream
        desktop = _read("jellyfin_mpv_shim", "integration",
                        constants.DESKTOP_ID + ".desktop")
        self.assertIn("Exec=jellyhome-player", desktop)
        self.assertIn("Icon=" + constants.DESKTOP_ID, desktop)
        self.assertIn("StartupWMClass=" + constants.DESKTOP_ID, desktop)
        self.assertIn("Name=" + constants.USER_APP_NAME, desktop)
        appdata = xml.dom.minidom.parseString(_read(
            "jellyfin_mpv_shim", "integration",
            constants.DESKTOP_ID + ".appdata.xml"))
        self.assertEqual(
            appdata.getElementsByTagName("id")[0].firstChild.data,
            constants.DESKTOP_ID)

    def test_the_console_script_is_the_forks(self):
        text = _read("pyproject.toml")
        self.assertIn('name = "jellyhome-player"', text)
        self.assertIn('jellyhome-player = "jellyfin_mpv_shim.mpv_shim:main"',
                      text)
        self.assertNotRegex(text, r"(?m)^jellyfin-mpv-shim\s*=")

    def test_ci_token_is_read_only(self):
        self.assertRegex(_read(".github", "workflows", "main.yml"),
                         r"(?m)^permissions:\n  contents: read")

    def test_the_mpris_bus_name_follows_the_app_id(self):
        from jellyfin_mpv_shim import mpris
        self.assertEqual(mpris.BUS_NAME,
                         "org.mpris.MediaPlayer2." + constants.DESKTOP_ID)

class FirstRunTest(unittest.TestCase):
    def test_public_build_has_no_prefilled_server(self):
        self.assertEqual(constants.DEFAULT_SERVER_URL, "")
        self.assertEqual(constants.prefilled_server_address(), "")

    def test_only_a_clean_https_address_is_prefilled(self):
        f = constants.prefilled_server_address
        self.assertEqual(f("https://media.example.org"),
                         "https://media.example.org")
        for bad in ("http://media.example.org", "ftp://x", "media.example.org",
                    "https://user:pw@media.example.org",
                    "https://media.example.org/?api_key=abc", "https://",
                    "https://[::1", None and "", ""):
            with self.subTest(bad=bad):
                self.assertEqual(f(bad), "")

if __name__ == "__main__":
    unittest.main()
