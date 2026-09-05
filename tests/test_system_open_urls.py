"""``system_open.open_url`` — the scheme allowlist, and the openers.

This function is handed strings the *server* composed: ``ExternalUrls`` on
a Jellyfin item, built by whatever metadata plugins that server runs. On the
other end is a desktop opener, which will cheerfully route ``file://``,
``smb://`` or a registered application scheme to whatever claims it. A shim
that browses a server it does not administer should not be a way for that
server to open arbitrary handlers on this machine, so the check is an
allowlist and it is tested as one — a denylist test would pass while the
next scheme somebody invents walked straight through.

The openers are exercised through ``_spawn``, which is where ``open_path``'s
own tests draw the line too: what is being asserted is *which argv we would
run*, not that a browser really started.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim import system_open  # noqa: E402


class UrlSchemeTest(unittest.TestCase):
    """What is refused, and what is not."""

    def setUp(self):
        self.spawned = []
        patches = [
            mock.patch.object(system_open, "_spawn",
                              lambda argv: self.spawned.append(argv) or True),
            mock.patch.object(system_open.shutil, "which",
                              lambda cmd: "/usr/bin/" + cmd
                              if cmd == "xdg-open" else None),
            mock.patch.object(system_open.os, "name", "posix"),
            mock.patch.object(system_open.sys, "platform", "linux"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_http_and_https_are_opened(self):
        for url in ("http://example.test/a", "https://example.test/a"):
            with self.subTest(url=url):
                self.assertEqual(system_open.open_url(url)[0], True)

    def test_everything_else_is_refused(self):
        """Not a denylist of known-bad schemes: anything outside the two."""
        for url in ("file:///etc/passwd",
                    "javascript:alert(1)",
                    "data:text/html,<script>",
                    "smb://host/share",
                    "ftp://host/x",
                    "vscode://vscode.git/clone?url=x",
                    "ms-settings:display",
                    "steam://run/440",
                    "\\\\host\\share",
                    "/etc/passwd",
                    "example.test/no-scheme"):
            with self.subTest(url=url):
                self.assertEqual(system_open.open_url(url), (False, None))
        self.assertEqual(self.spawned, [], "a refused url reached an opener")

    def test_a_scheme_with_no_host_is_refused(self):
        """``https:///etc/passwd`` parses with the right scheme and no host,
        and is not a link to anywhere."""
        self.assertEqual(system_open.open_url("https:///etc/passwd"),
                         (False, None))
        self.assertEqual(self.spawned, [])

    def test_the_scheme_is_matched_case_insensitively(self):
        """URLs are not case-sensitive in the scheme, and a server that
        writes ``HTTPS://`` is not sending a different kind of link."""
        self.assertEqual(system_open.open_url("HTTPS://example.test/a")[0],
                         True)

    def test_nothing_at_all_is_refused_quietly(self):
        for url in (None, "", "   "):
            with self.subTest(url=url):
                self.assertEqual(system_open.open_url(url), (False, None))

    def test_surrounding_whitespace_is_stripped(self):
        system_open.open_url("  https://example.test/a  ")
        self.assertEqual(self.spawned, [["xdg-open", "https://example.test/a"]])

    def test_the_url_reaches_the_opener_intact(self):
        """A query string and a fragment are part of the link, and an opener
        is handed one argument, not a shell string."""
        url = "https://example.test/t?q=a+b&x=1#frag"
        system_open.open_url(url)
        self.assertEqual(self.spawned, [["xdg-open", url]])

    def test_a_malformed_url_is_an_answer_rather_than_a_crash(self):
        """This runs from a click handler on the render loop."""
        self.assertEqual(system_open.open_url("https://[oops"), (False, None))


class UrlOpenerTest(unittest.TestCase):
    """Which opener each platform reaches for."""

    def test_linux_walks_the_opener_list_in_order(self):
        tried = []

        def which(cmd):
            tried.append(cmd)
            return "/usr/bin/" + cmd if cmd == "kde-open" else None

        with mock.patch.object(system_open, "_spawn", lambda argv: True), \
                mock.patch.object(system_open.shutil, "which", which), \
                mock.patch.object(system_open.os, "name", "posix"), \
                mock.patch.object(system_open.sys, "platform", "linux"):
            ok, method = system_open.open_url("https://example.test/a")
        self.assertEqual((ok, method), (True, "kde-open"))
        self.assertEqual(tried[:2], ["xdg-open", "gio"],
                         "xdg-open must be tried first -- it is what a "
                         "Flatpak's portal intercepts")

    def test_gio_takes_its_own_argv(self):
        spawned = []
        with mock.patch.object(system_open, "_spawn",
                               lambda argv: spawned.append(argv) or True), \
                mock.patch.object(system_open.shutil, "which",
                                  lambda cmd: "/usr/bin/gio"
                                  if cmd == "gio" else None), \
                mock.patch.object(system_open.os, "name", "posix"), \
                mock.patch.object(system_open.sys, "platform", "linux"):
            system_open.open_url("https://example.test/a")
        self.assertEqual(spawned, [["gio", "open", "https://example.test/a"]])

    def test_no_opener_at_all_is_a_reported_failure(self):
        with mock.patch.object(system_open.shutil, "which", lambda cmd: None), \
                mock.patch.object(system_open.os, "name", "posix"), \
                mock.patch.object(system_open.sys, "platform", "linux"):
            self.assertEqual(system_open.open_url("https://example.test/a"),
                             (False, None))

    def test_macos_uses_open(self):
        spawned = []
        with mock.patch.object(system_open, "_spawn",
                               lambda argv: spawned.append(argv) or True), \
                mock.patch.object(system_open.os, "name", "posix"), \
                mock.patch.object(system_open.sys, "platform", "darwin"):
            ok, method = system_open.open_url("https://example.test/a")
        self.assertEqual((ok, method), (True, "open"))
        self.assertEqual(spawned, [["open", "https://example.test/a"]])

    def test_a_url_never_has_to_exist_on_disk(self):
        """The one thing that separates this from ``open_path``: that one
        rejects a target that is not a file, which is every URL."""
        self.assertEqual(system_open.open_path("https://example.test/a"),
                         (False, None))


if __name__ == "__main__":
    unittest.main()
