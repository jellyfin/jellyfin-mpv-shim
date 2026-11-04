"""JellyHome Player identity, packaging, autostart, import and first-run.

Windows/Flatpak behaviour is checked as far as a Linux host allows: the
packaging files are parsed and cross-checked rather than built, and the
registry path is exercised against a fake ``winreg``.
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
import types
import unittest
import xml.dom.minidom
from unittest import mock

from tests import _tmpdirs

sys.argv = ["test"]

from jellyfin_mpv_shim import autostart, constants, upstream_import   # noqa: E402

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
        # The autostart value the app writes is removed on uninstall.
        self.assertIn("uninsdeletevalue", iss)
        self.assertIn('ValueName: "{#MyAppName}"', iss)
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

    def test_no_first_party_source_references_the_removed_lookup(self):
        """Only a tripwire; the real guard is
        test_jellyhome_privacy.LocalityTest.test_no_third_party_is_ever_contacted."""
        hits = []
        for base, dirs, files in os.walk(os.path.join(ROOT, "jellyfin_mpv_shim")):
            dirs[:] = [d for d in dirs if d not in ("messages", "locale",
                                                    "__pycache__")]
            for name in files:
                if name.endswith((".py", ".lua")):
                    path = os.path.join(base, name)
                    with open(path, encoding="utf-8") as fh:
                        if re.search(r"checkip|amazonaws", fh.read(), re.I):
                            hits.append(path)
        self.assertEqual(hits, [])


class AutostartTest(unittest.TestCase):
    def setUp(self):
        self.home = _tmpdirs.tmpdir("jh-autostart-")
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        p = mock.patch.object(sys, "platform", "linux")
        p.start()
        self.addCleanup(p.stop)
        from jellyfin_mpv_shim import hostinfo
        p = mock.patch.object(hostinfo, "is_flatpak", lambda: False)
        p.start()
        self.addCleanup(p.stop)

    def _file(self):
        return os.path.join(self.home, "autostart",
                            constants.DESKTOP_ID + ".desktop")

    def test_off_by_default_and_nothing_written(self):
        from jellyfin_mpv_shim.conf import Settings
        self.assertFalse(Settings().start_with_computer)
        self.assertFalse(autostart.is_enabled())

    def test_enable_then_disable_removes_exactly_our_entry(self):
        other = os.path.join(self.home, "autostart", "other.desktop")
        os.makedirs(os.path.dirname(other))
        open(other, "w", encoding="utf-8").close()
        self.assertEqual(autostart.set_enabled(True), (True, ""))
        text = _text(self._file())
        self.assertIn("--minimized", text)
        self.assertIn("Name=" + constants.USER_APP_NAME, text)
        self.assertTrue(autostart.is_enabled())
        self.assertEqual(autostart.set_enabled(False), (True, ""))
        self.assertFalse(os.path.exists(self._file()))
        self.assertTrue(os.path.exists(other), "unrelated entry was removed")
        self.assertEqual(autostart.set_enabled(False), (True, ""))  # idempotent

    def test_flatpak_and_macos_refuse_to_enable_with_a_reason(self):
        from jellyfin_mpv_shim import hostinfo
        with mock.patch.object(hostinfo, "is_flatpak", lambda: True):
            ok, why = autostart.set_enabled(True)
        self.assertFalse(ok)
        self.assertIn("Flatpak", why)
        self.assertFalse(os.path.exists(self._file()))
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertFalse(autostart.set_enabled(True)[0])

    def test_reconcile_removes_a_stale_entry_when_the_setting_is_off(self):
        autostart.set_enabled(True)
        autostart.reconcile(False)
        self.assertFalse(os.path.exists(self._file()))

    def test_reconcile_does_nothing_when_off_and_absent(self):
        with mock.patch.object(autostart, "set_enabled") as se:
            autostart.reconcile(False)
        se.assert_not_called()

    def test_a_percent_in_the_path_cannot_inject_a_field_code(self):
        with mock.patch.object(autostart, "_launch_args",
                               lambda: ["/opt/a%f b/app", "--minimized"]):
            self.assertIn("%%f", autostart._desktop_text())

    def test_windows_writes_and_deletes_one_run_value(self):
        store = {}

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        fake = types.SimpleNamespace(
            HKEY_CURRENT_USER="HKCU", REG_SZ=1,
            CreateKey=lambda root, sub: (store.setdefault("sub", (root, sub)),
                                         Key())[1],
            OpenKey=lambda root, sub: Key(),
            SetValueEx=lambda k, n, r, t, v: store.__setitem__(n, v),
            DeleteValue=lambda k, n: (store.pop(n) if n in store else
                                      (_ for _ in ()).throw(
                                          FileNotFoundError())),
            QueryValueEx=lambda k, n: (store[n], 1) if n in store
            else (_ for _ in ()).throw(FileNotFoundError()))
        with mock.patch.dict(sys.modules, {"winreg": fake}), \
                mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable",
                                  r"C:\Users\me\AppData\Local\Programs"
                                  r"\JellyHome Player\JellyHomePlayer.exe"):
            self.assertTrue(autostart.set_enabled(True)[0])
            self.assertEqual(store["sub"][1], autostart.RUN_KEY)
            value = store[constants.USER_APP_NAME]
            self.assertTrue(value.startswith('"C:'), value)
            self.assertTrue(value.endswith("--minimized"))
            self.assertTrue(autostart.is_enabled())
            self.assertTrue(autostart.set_enabled(False)[0])
            self.assertNotIn(constants.USER_APP_NAME, store)
            self.assertTrue(autostart.set_enabled(False)[0])


class UpstreamImportTest(unittest.TestCase):
    def _dirs(self):
        return (_tmpdirs.tmpdir("jh-up-src-"), _tmpdirs.tmpdir("jh-up-dst-"))

    def test_copies_preferences_but_never_identity_or_logins(self):
        src, dst = self._dirs()
        with open(os.path.join(src, "conf.json"), "w", encoding="utf-8") as fh:
            json.dump({"client_uuid": "dev-1", "check_updates": True,
                       "discord_presence": True, "ui_scale": 1.5,
                       "config_version": 6}, fh)
        for name in ("users.json", "cred.json", "users.json.bak"):
            with open(os.path.join(src, name), "w", encoding="utf-8") as fh:
                fh.write('{"token": "SECRET"}')
        with open(os.path.join(src, "mpv.conf"), "w", encoding="utf-8") as fh:
            fh.write("hwdec=auto\n")
        report = upstream_import.run(src, dst)
        got = _json(os.path.join(dst, "conf.json"))
        self.assertNotIn("client_uuid", got)
        self.assertIs(got["check_updates"], False)
        self.assertIs(got["discord_presence"], False)
        self.assertEqual(got["ui_scale"], 1.5)
        self.assertEqual(_text(os.path.join(dst, "mpv.conf")), "hwdec=auto\n")
        self.assertEqual(sorted(os.listdir(dst)),
                         ["conf.json", "mpv.conf"])
        self.assertIn("NOT copied", " ".join(report))
        if os.name == "posix":
            self.assertEqual(os.stat(os.path.join(dst, "conf.json"))
                             .st_mode & 0o077, 0)

    def test_never_overwrites(self):
        src, dst = self._dirs()
        for d, v in ((src, 1.5), (dst, 2.0)):
            with open(os.path.join(d, "conf.json"), "w", encoding="utf-8") as fh:
                json.dump({"ui_scale": v}, fh)
        with open(os.path.join(dst, "mpv.conf"), "w", encoding="utf-8") as fh:
            fh.write("mine\n")
        with open(os.path.join(src, "mpv.conf"), "w", encoding="utf-8") as fh:
            fh.write("theirs\n")
        upstream_import.run(src, dst)
        self.assertEqual(_json(os.path.join(dst, "conf.json"))["ui_scale"],
                         2.0)
        self.assertEqual(_text(os.path.join(dst, "mpv.conf")), "mine\n")

    def test_missing_or_broken_source_is_reported_not_raised(self):
        src, dst = self._dirs()
        self.assertIn("No upstream", upstream_import.run(
            os.path.join(src, "nope"), dst)[0])
        with open(os.path.join(src, "conf.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertTrue(any("Could not read" in l
                            for l in upstream_import.run(src, dst)))
        self.assertFalse(os.path.exists(os.path.join(dst, "conf.json")))

    def test_same_directory_is_a_no_op(self):
        src, _ = self._dirs()
        self.assertIn("same directory", upstream_import.run(src, src)[0])

    def test_the_command_is_registered(self):
        from jellyfin_mpv_shim import args
        self.assertIn("import-upstream", args.COMMANDS)


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

    def test_a_bare_address_never_silently_becomes_plain_http_on_the_internet(
            self):
        from jellyfin_mpv_shim.clients import ClientManager
        norm = ClientManager._normalize_server
        # Public names/addresses: https, and no 8096 injected.
        self.assertEqual(norm("media.example.org"), "https://media.example.org")
        self.assertEqual(norm("93.184.216.34"), "https://93.184.216.34")
        # Home-network spellings keep the historical http default.
        self.assertEqual(norm("192.168.1.5"), "http://192.168.1.5:8096")
        self.assertEqual(norm("jellyfin"), "http://jellyfin:8096")
        self.assertEqual(norm("nas.local"), "http://nas.local:8096")
        self.assertEqual(norm("localhost"), "http://localhost:8096")
        # An explicit scheme is never changed.
        self.assertEqual(norm("http://media.example.org:80"),
                         "http://media.example.org:80")
        self.assertEqual(norm("https://media.example.org/"),
                         "https://media.example.org")


if __name__ == "__main__":
    unittest.main()
