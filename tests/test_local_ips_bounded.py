"""`get_local_ips` must not hold the connect path hostage to a slow resolver.

It runs at the top of `ClientManager._connect_all` -- app start and every user
switch -- only to rank saved servers by subnet. On a machine whose hostname is
missing from /etc/hosts the lookup takes a resolver timeout (5 s measured), so
every start waited that long before the first connect, and the two
tests/integration SwitchUserInvalidatesInFlightAuth tests (which give the
switch 5 s) failed on it. The primary address comes from a UDP route lookup that
sends nothing; the hostname's extra addresses are optional.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import ipaddress
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim import clients  # noqa: E402


class FakeUdpSocket:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, _addr):
        pass

    def getsockname(self):
        return ("192.168.7.20", 12345)


class _LookupFixture:
    def setUp(self):
        # Module-level single-flight state: start every test from none, and
        # leave none behind for the next one.
        self._reset()
        self.addCleanup(self._reset)

    @staticmethod
    def _reset():
        clients._hostname_thread = None
        clients._hostname_ips = []

    def _patched(self, getaddrinfo):
        return mock.patch.multiple(
            clients.socket, socket=lambda *a, **k: FakeUdpSocket(),
            getaddrinfo=getaddrinfo, gethostname=lambda: "somehost")


class BoundedLookupTest(_LookupFixture, unittest.TestCase):
    def test_a_slow_resolver_is_waited_for_only_briefly(self):
        release = threading.Event()

        def slow(*_a, **_k):
            release.wait(10)
            return [(None, None, None, "", ("10.1.2.3", 0))]

        self.addCleanup(release.set)
        with self._patched(slow):
            t0 = time.monotonic()
            ips = clients.get_local_ips(hostname_timeout=0.3)
            took = time.monotonic() - t0
        self.assertLess(took, 2.0)
        self.assertEqual(ips, [ipaddress.ip_address("192.168.7.20")],
                         "the primary address must survive a slow lookup")

    def test_a_fast_resolver_still_adds_its_private_addresses(self):
        def fast(*_a, **_k):
            return [(None, None, None, "", ("10.1.2.3", 0)),
                    (None, None, None, "", ("8.8.4.4", 0)),         # public: dropped
                    (None, None, None, "", ("192.168.7.20", 0))]    # duplicate

        with self._patched(fast):
            ips = clients.get_local_ips()
        self.assertEqual(ips, [ipaddress.ip_address("192.168.7.20"),
                               ipaddress.ip_address("10.1.2.3")])

    def test_a_failing_resolver_is_not_an_error(self):
        import socket

        def boom(*_a, **_k):
            raise socket.gaierror("no such host")

        with self._patched(boom):
            self.assertEqual(clients.get_local_ips(),
                             [ipaddress.ip_address("192.168.7.20")])


class LookupResourceBoundsTest(_LookupFixture, unittest.TestCase):
    """A stuck resolver must cost one thread, however often we connect."""

    def test_repeated_calls_with_a_stuck_resolver_start_one_lookup(self):
        release = threading.Event()
        calls = []

        def stuck(*_a, **_k):
            calls.append(threading.current_thread().name)
            release.wait(30)
            return [(None, None, None, "", ("10.1.2.3", 0))]

        self.addCleanup(release.set)
        before = threading.active_count()
        with self._patched(stuck):
            t0 = time.monotonic()
            for _ in range(40):                    # 40 user switches
                ips = clients.get_local_ips(hostname_timeout=0.01)
                self.assertEqual(ips, [ipaddress.ip_address("192.168.7.20")])
            took = time.monotonic() - t0
        self.assertEqual(len(calls), 1, "one resolver call, not one per switch")
        self.assertLessEqual(threading.active_count() - before, 1)
        self.assertLess(took, 3.0)

    def test_a_late_result_refreshes_the_next_call_only(self):
        release = threading.Event()

        def late(*_a, **_k):
            release.wait(30)
            return [(None, None, None, "", ("10.9.9.9", 0))]

        self.addCleanup(release.set)
        with self._patched(late):
            first = clients.get_local_ips(hostname_timeout=0.01)
            self.assertEqual(first, [ipaddress.ip_address("192.168.7.20")])
            release.set()
            clients._hostname_thread.join(5)
            later = clients.get_local_ips(hostname_timeout=0.5)
        # The value returned earlier is a plain list nobody mutates afterwards.
        self.assertEqual(first, [ipaddress.ip_address("192.168.7.20")])
        self.assertIn(ipaddress.ip_address("10.9.9.9"), later)

    def test_lookup_results_never_include_public_addresses(self):
        with self._patched(lambda *a, **k: [(None, None, None, "",
                                             ("8.8.4.4", 0))]):
            self.assertEqual(clients.get_local_ips(),
                             [ipaddress.ip_address("192.168.7.20")])

    def test_a_stuck_resolver_does_not_hold_the_process_open(self):
        import subprocess
        code = (
            "import sys, time, threading; sys.argv=[sys.argv[0]]\n"
            "from jellyfin_mpv_shim import clients\n"
            "def stuck(*a, **k):\n"
            "    time.sleep(60)\n"
            "clients.socket.getaddrinfo = stuck\n"
            "t0 = time.monotonic()\n"
            "clients.get_local_ips(hostname_timeout=0.05)\n"
            "print('returned', round(time.monotonic() - t0, 2))\n")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        t0 = time.monotonic()
        out = subprocess.run([sys.executable, "-c",
                              "import sys; sys.path.insert(0, %r)\n" % root
                              + code],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        self.assertIn("returned", out.stdout)
        self.assertLess(time.monotonic() - t0, 15,
                        "interpreter exit waited for the stuck lookup")


class LookupCannotTouchTrustTest(unittest.TestCase):
    """The hostname lookup only ranks servers; direct-path trust is separate."""

    def test_trust_does_not_depend_on_the_lookup(self):
        import inspect
        from jellyfin_mpv_shim import network_locality
        src = inspect.getsource(network_locality)
        self.assertNotIn("get_local_ips", src)
        self.assertNotIn("_hostname_", src)
        # ...and a client we cannot classify is untrusted, lookup or not.
        self.assertEqual(network_locality.for_client(None),
                         network_locality.REMOTE_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
