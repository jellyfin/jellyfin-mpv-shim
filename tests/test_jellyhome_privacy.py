"""JellyHome Player privacy regressions: update checks and server locality.

Everything here runs against mocked network boundaries; no test needs a
live service. The properties:

* No release request is ever made while automatic checks are off, including
  after migrating an upstream config that says `check_updates: true`.
* "Check for Updates Now" works regardless of that setting, but makes no
  request at all when no release repository is configured, and never follows
  a redirect into upstream.
* Deciding whether a server is local contacts nobody but the Jellyfin server
  itself, fails to Remote, and the user's *Local* choice never grants
  direct-path trust.
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
import sys
import threading
import unittest
from unittest import mock

from tests import _tmpdirs

sys.argv = ["test"]

import jellyfin_mpv_shim.update_check as uc                    # noqa: E402
from jellyfin_mpv_shim import network_locality as nl           # noqa: E402
from jellyfin_mpv_shim.conf import CONFIG_VERSION, Settings    # noqa: E402


class _Player:
    def show_text(self, *a):
        pass


class _Redirect:
    def __init__(self, location, status=302):
        self.status_code = status
        self.headers = {"location": location}


def _boom(*a, **kw):
    raise AssertionError("unexpected network request: %r" % (a,))


class AutomaticUpdateChecksTest(unittest.TestCase):
    def test_default_is_off(self):
        self.assertFalse(Settings().check_updates)

    def test_no_request_while_off(self):
        chk = uc.UpdateChecker(_Player())
        with mock.patch.object(uc.settings, "check_updates", False), \
                mock.patch.object(uc.requests, "get", _boom):
            for _ in range(3):  # startup, playback, "periodic": all one path
                chk.check()
        self.assertIsNone(chk.new_version)

    def test_upstream_true_does_not_survive_migration(self):
        s = Settings()
        s.config_version = 6
        s.check_updates = True  # what an upstream conf.json carries
        self.assertTrue(s._migrate({"check_updates": True}))
        self.assertFalse(s.check_updates)
        self.assertEqual(s.config_version, CONFIG_VERSION)

    def test_opt_in_made_in_jellyhome_persists_across_loads(self):
        path = _tmpdirs.tmpfile("conf.json", "jh-privacy-")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"config_version": 6, "check_updates": True}, fh)
        first = Settings()
        first.load(path)
        self.assertFalse(first.check_updates, "inherited true was honoured")
        first.check_updates = True       # the user's explicit opt-in
        first.save()
        second = Settings()
        second.load(path)
        self.assertTrue(second.check_updates, "explicit opt-in was reset")

    def test_fresh_config_is_off_and_stamped_current(self):
        path = _tmpdirs.tmpfile("fresh.json", "jh-privacy-")
        s = Settings()
        s.load(path)
        self.assertFalse(s.check_updates)
        self.assertEqual(s.config_version, CONFIG_VERSION)


class ManualUpdateCheckTest(unittest.TestCase):
    OWNER = "braisravina"

    def _checker(self, table):
        asked = []

        def get(url, **kw):
            asked.append(url)
            if url not in table:
                raise AssertionError("unexpected request " + url)
            return table[url]

        chk = uc.UpdateChecker(_Player())
        patches = [mock.patch.object(uc.requests, "get", get),
                   mock.patch.object(uc.settings, "check_updates", False)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return chk, asked

    def test_works_while_automatic_checks_are_off(self):
        tag = uc.release_url + "tag/v99.0.0"
        chk, asked = self._checker(
            {uc.release_url + "latest": _Redirect(tag)})
        self.assertEqual(chk.check_now(), ("newer", "99.0.0"))
        self.assertEqual(asked, [uc.release_url + "latest"])

    def test_up_to_date(self):
        tag = uc.release_url + "tag/v0.0.1"
        chk, _ = self._checker({uc.release_url + "latest": _Redirect(tag)})
        self.assertEqual(chk.check_now(), ("current", None))

    def test_a_failure_is_an_error_not_up_to_date(self):
        chk, _ = self._checker({})
        self.assertEqual(chk.check_now()[0], "error")

    def test_nothing_is_requested_without_a_configured_repository(self):
        with mock.patch.object(uc, "release_urls", []), \
                mock.patch.object(uc.requests, "get", _boom):
            self.assertEqual(uc.UpdateChecker(_Player()).check_now(),
                             ("unconfigured", None))

    def test_the_default_endpoint_is_the_forks_not_upstreams(self):
        self.assertIn(self.OWNER, uc.release_url)
        self.assertNotIn("jellyfin/jellyfin-mpv-shim", uc.release_url)

    def test_a_redirect_into_upstream_is_refused(self):
        upstream = "https://github.com/jellyfin/jellyfin-mpv-shim/releases/"
        chk, asked = self._checker({
            uc.release_url + "latest": _Redirect(upstream + "latest", 301)})
        self.assertEqual(chk.check_now()[0], "error")
        self.assertEqual(asked, [uc.release_url + "latest"],
                         "upstream was contacted")

    def test_a_redirect_to_another_host_is_refused(self):
        chk, asked = self._checker({
            uc.release_url + "latest":
                _Redirect("https://evil.invalid/x/y/releases/tag/v9.9.9")})
        self.assertEqual(chk.check_now()[0], "error")
        self.assertEqual(len(asked), 1)

    def test_the_request_carries_nothing_identifying(self):
        seen = {}

        def get(url, **kw):
            seen.update(kw)
            return _Redirect(uc.release_url + "tag/v0.0.1")

        with mock.patch.object(uc.requests, "get", get):
            uc.UpdateChecker(_Player()).check_now()
        self.assertFalse(seen.get("allow_redirects", True))
        self.assertIn("timeout", seen)
        for key in ("headers", "params", "data", "json", "auth", "cookies"):
            self.assertNotIn(key, seen)


class LocalityTest(unittest.TestCase):
    @staticmethod
    def _resolver(*ips):
        def resolve(host, port):
            if not ips:
                raise OSError("no such host")
            return [(0, 0, 0, "", (ip, 0)) for ip in ips]
        return resolve

    def _detect(self, address, *ips, ask=None):
        return nl.detect(address, ask, resolver=self._resolver(*ips))

    def test_no_third_party_is_ever_contacted(self):
        with mock.patch("requests.get", _boom), \
                mock.patch("requests.Session.request", _boom), \
                mock.patch("urllib.request.urlopen", _boom):
            self._detect("https://media.example", "93.184.216.34")
            self._detect("https://media.example", "192.168.1.5")
            self._detect("https://media.example", "2606:4700::1",
                         ask=lambda: False)

    def test_private_addresses_are_local_and_trusted(self):
        for ip in ("192.168.1.5", "10.0.0.2", "172.16.4.4", "127.0.0.1",
                   "169.254.1.1", "fd00::1", "fe80::1"):
            with self.subTest(ip=ip):
                got = self._detect("http://h:8096", ip)
                self.assertEqual((got.local, got.direct_path_trusted),
                                 (True, True))

    def test_a_public_address_is_remote_when_the_server_does_not_say_otherwise(
            self):
        self.assertEqual(self._detect("https://h", "93.184.216.34"),
                         nl.Locality(False, False, "public-address"))

    def test_cgnat_and_vpn_ranges_are_remote(self):
        self.assertFalse(self._detect("https://h", "100.100.1.1").local)

    def test_hairpin_nat_uses_the_servers_own_answer_for_bandwidth_only(self):
        for ip in ("93.184.216.34", "2606:4700::1"):
            with self.subTest(ip=ip):
                got = self._detect("https://h", ip, ask=lambda: True)
                self.assertTrue(got.local)
                self.assertFalse(got.direct_path_trusted,
                                 "a server claim must never grant file access")

    def test_failures_and_ambiguity_resolve_to_remote(self):
        def explode():
            raise OSError("timeout")

        for case in (self._detect("https://h", "93.184.216.34",
                                  ask=explode),
                     self._detect("https://h", "93.184.216.34",
                                  ask=lambda: None),
                     self._detect("https://h"),               # no DNS
                     self._detect(None, "192.168.1.1"),
                     self._detect("", "192.168.1.1"),
                     self._detect("http://[::1", "192.168.1.1")):
            self.assertEqual((case.local, case.direct_path_trusted),
                             (False, False), case)

    def test_a_mixed_answer_is_local_for_bandwidth_but_not_trusted(self):
        got = self._detect("https://h", "2606:4700::1", "192.168.3.133")
        self.assertEqual((got.local, got.direct_path_trusted), (True, False))

    def test_the_override(self):
        private = self._detect("http://h", "192.168.1.5")
        public = self._detect("https://h", "93.184.216.34")
        # Auto passes the detection through.
        self.assertEqual(nl.apply_override("auto", private), private)
        # Local lifts the bandwidth cap but grants no trust that Auto did not.
        got = nl.apply_override("local", public)
        self.assertEqual((got.local, got.direct_path_trusted), (True, False))
        # Remote overrides even a private address, trust included.
        got = nl.apply_override("remote", private)
        self.assertEqual((got.local, got.direct_path_trusted), (False, False))
        # Garbage is Auto.
        self.assertEqual(nl.apply_override("LAN!", private), private)

    def test_normalize(self):
        self.assertEqual(nl.normalize_connection_type(" Remote "), "remote")
        for bad in (None, "", "everything", 3):
            self.assertEqual(nl.normalize_connection_type(bad), "auto")

    def test_for_client_never_raises_and_fails_remote(self):
        class Broken:
            @property
            def config(self):
                raise RuntimeError("boom")

        self.assertEqual(nl.for_client(Broken()), nl.REMOTE_UNKNOWN)

    def test_for_client_asks_only_the_jellyfin_server(self):
        calls = []

        class Jf:
            def get_endpoint_info(self):
                calls.append("endpoint")
                return {"IsInNetwork": True}

        class Cfg:
            data = {"auth.server": "https://media.example"}

        class Client:
            config = Cfg()
            jellyfin = Jf()

        nl.forget()
        with mock.patch("socket.getaddrinfo", self._resolver("93.184.216.34")), \
                mock.patch("requests.get", _boom):
            got = nl.for_client(Client(), "auto")
        self.assertEqual((got.local, got.direct_path_trusted), (True, False))
        self.assertEqual(calls, ["endpoint"])

    def test_the_answer_is_cached_per_client(self):
        cache = nl.LocalityCache(clock=lambda: 0.0)
        n = []

        def compute():
            n.append(1)
            return nl.Locality(True, True, "private-address")

        cache.get("k", "http://h", compute)
        cache.get("k", "http://h", compute)
        self.assertEqual(len(n), 1)
        cache.get("k", "http://other", compute)
        self.assertEqual(len(n), 2)


class ConnectionTypePersistenceTest(unittest.TestCase):
    def _manager(self):
        from jellyfin_mpv_shim import clients
        mgr = clients.ClientManager.__new__(clients.ClientManager)
        mgr._switch_lock = threading.RLock()
        mgr._client_lock = threading.RLock()
        mgr.clients = {}
        mgr.credentials = [{"uuid": "u1", "Id": "s1", "address": "http://h"},
                           {"uuid": "u2", "Id": "s2", "address": "http://k"}]
        saved = []
        mgr.save_credentials = lambda: saved.append(
            [dict(c) for c in mgr.credentials])
        return mgr, saved

    def test_default_is_auto(self):
        mgr, _ = self._manager()
        self.assertEqual(mgr.connection_type("u1"), "auto")
        self.assertEqual(mgr.connection_type("nobody"), "auto")

    def test_it_is_saved_per_server_with_its_credentials(self):
        mgr, saved = self._manager()
        self.assertTrue(mgr.set_connection_type("u2", "REMOTE"))
        self.assertEqual(mgr.connection_type("u2"), "remote")
        self.assertEqual(mgr.connection_type("u1"), "auto")
        self.assertEqual(saved[-1][1]["connection_type"], "remote")
        self.assertNotIn("connection_type", saved[-1][0])

    def test_junk_is_stored_as_auto_and_unknown_server_is_refused(self):
        mgr, saved = self._manager()
        self.assertTrue(mgr.set_connection_type("u1", "banana"))
        self.assertEqual(mgr.connection_type("u1"), "auto")
        self.assertFalse(mgr.set_connection_type("nope", "local"))

    def test_an_existing_credential_without_the_key_loads_as_auto(self):
        mgr, _ = self._manager()
        self.assertEqual(mgr.connection_type("u1"), "auto")

    def test_connection_type_survives_clean_credentials_for_save(self):
        from jellyfin_mpv_shim import clients
        out = clients.clean_credentials_for_save(
            [{"uuid": "u1", "connected": True, "connection_type": "local"}])
        self.assertEqual(out, [{"uuid": "u1", "connection_type": "local"}])

    def test_locality_for_a_missing_client_is_remote(self):
        mgr, _ = self._manager()
        got = mgr.locality_for_client(None)
        self.assertEqual((got.local, got.direct_path_trusted), (False, False))


class LogRedactionTest(unittest.TestCase):
    """Secrets are removed whatever case or alphabet they arrive in."""

    @staticmethod
    def _clean(text):
        from jellyfin_mpv_shim.log_utils import sanitize
        return sanitize(text)

    def test_tokens_in_any_case_are_fully_redacted(self):
        for text, secret in (
                ("GET /x?api_key=0123ABCDEF&a=1", "ABCDEF"),
                ("GET /x?ApiKey=0123ABCDEFxyz-_&a=1", "xyz"),
                ("GET /x?apikey=ab12cd", "ab12cd"),
                ("{'AccessToken': 'ABCDEF0123'}", "ABCDEF0123"),
                ("{'X-Emby-Token': 'ZZtop99'}", "ZZtop99"),
                ('Authorization: MediaBrowser Token="Qw-9_x"', "Qw-9_x"),
                ("server https://admin:hunter2@media.example/", "hunter2")):
            with self.subTest(text=text):
                out = self._clean(text)
                self.assertNotIn(secret, out)
                self.assertIn("REDACTED", out)

    def test_ordinary_text_and_the_key_spelling_survive(self):
        self.assertEqual(self._clean("GET /x?ApiKey=abc&y=1"),
                         "GET /x?ApiKey=REDACTED&y=1")
        self.assertEqual(self._clean("https://media.example:8096/web/"),
                         "https://media.example:8096/web/")
        self.assertEqual(self._clean("Playing 12:30:45 at 10:5"),
                         "Playing 12:30:45 at 10:5")


if __name__ == "__main__":
    unittest.main()
