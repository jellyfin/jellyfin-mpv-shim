"""`tools/bench_image_loading.py` can still read a config directory.

The benchmark is a diagnostic handed to people reporting slow libraries, so
it is run by someone who has no idea what it does, on a config this branch
has never seen, and any failure they hit is a failure to collect the
evidence. The part that will break silently is credential reading: the store
has already changed shape once (cred.json -> users.json), and if it changes
again this tool reads no servers and says "sign in with the app first" to
somebody who is signed in.

The measurements themselves need a server and are not tested here.
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
import sys
import shutil
import tempfile
import unittest

sys.argv = [sys.argv[0]]

import jellyfin_mpv_shim  # noqa: E402

PKG = os.path.dirname(os.path.abspath(jellyfin_mpv_shim.__file__))
TOOLS = os.path.join(os.path.dirname(PKG), "tools")


def load_tool():
    import importlib.util
    path = os.path.join(TOOLS, "bench_image_loading.py")
    spec = importlib.util.spec_from_file_location("bench_image_loading", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LoadServersTest(unittest.TestCase):
    def setUp(self):
        self.tool = load_tool()
        self.dir = tempfile.mkdtemp(prefix="bench-cfg-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, name, data):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    @staticmethod
    def cred(name, address="http://h:8096", token="t"):
        return {"address": address, "Name": name, "AccessToken": token,
                "UserId": "u"}

    def test_reads_the_current_store(self):
        self.write("users.json", {"active": "u1", "users": [
            {"id": "u1", "credentials": [self.cred("Casper")]}]})
        got = self.tool.load_servers(self.dir)
        self.assertEqual([c["Name"] for c in got], ["Casper"])

    def test_the_active_user_comes_first(self):
        """Whoever is signed in is the one whose servers are on screen, and
        so the one a report is about."""
        self.write("users.json", {"active": "u2", "users": [
            {"id": "u1", "credentials": [self.cred("Other")]},
            {"id": "u2", "credentials": [self.cred("Active")]}]})
        got = self.tool.load_servers(self.dir)
        self.assertEqual([c["Name"] for c in got], ["Active", "Other"])

    def test_reads_the_legacy_store(self):
        """A config directory that has not run 3.0 yet has only cred.json,
        and its owner is exactly the sort of person filing this report."""
        self.write("cred.json", {"Servers": [self.cred("Old")]})
        self.assertEqual([c["Name"] for c in self.tool.load_servers(self.dir)],
                         ["Old"])

    def test_the_new_store_wins_over_the_legacy_one(self):
        # Both exist after a migration; the old one is a stale copy.
        self.write("users.json", {"active": "u1", "users": [
            {"id": "u1", "credentials": [self.cred("New")]}]})
        self.write("cred.json", {"Servers": [self.cred("Old")]})
        self.assertEqual([c["Name"] for c in self.tool.load_servers(self.dir)],
                         ["New"])

    def test_entries_without_a_token_are_skipped(self):
        """A server record can exist with no token — signed out, or a failed
        add. Benchmarking it would 401 on every request and read as a slow
        server."""
        self.write("users.json", {"active": "u1", "users": [{"id": "u1",
                   "credentials": [{"address": "http://h:8096"},
                                   self.cred("Good")]}]})
        self.assertEqual([c["Name"] for c in self.tool.load_servers(self.dir)],
                         ["Good"])

    def test_an_empty_directory_is_not_an_error(self):
        self.assertEqual(self.tool.load_servers(self.dir), [])


class DurationTest(unittest.TestCase):
    """The verdict lines are the whole output for most readers."""

    def setUp(self):
        self.tool = load_tool()

    def test_reads_as_english(self):
        self.assertEqual(self.tool._duration(45), "45 seconds")
        self.assertEqual(self.tool._duration(600), "10.0 minutes")
        self.assertEqual(self.tool._duration(7200), "2.0 hours")


if __name__ == "__main__":
    unittest.main()
