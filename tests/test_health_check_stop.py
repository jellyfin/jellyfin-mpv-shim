"""Quitting does not wait out a health check's network call.

Under a stalled network (nothing refused, nothing answering) a health
check's /Sessions query runs about 80 s: a 10 s read timeout, three urllib3
attempts, twice. ClientManager.stop joined that thread without a bound, so
a quit landing mid-check outlived exit_watchdog's 20 s deadline and the app
was killed with rc 1 (e2e scenario 8, AStalledNetworkTest). The thread is a
daemon and a check that finishes after stop() sees is_stopping, so it is left
to end as a straggler, which exit_watchdog.finish reports.
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
import threading
import time
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

import jellyfin_mpv_shim.clients as clients  # noqa: E402


class QuitDuringAHealthCheckTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(clients.settings,
                                    "health_check_interval", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls = 0

        def parked_check():
            self.calls += 1
            self.entered.set()
            self.release.wait(30)        # a network call that is not answering

        self.manager = clients.ClientManager()
        self.manager.health_check = clients.PeriodicHealthCheck(parked_check)
        self.thread = self.manager.health_check
        self.thread.start()
        self.addCleanup(self.thread.join, 30)
        self.addCleanup(self.release.set)

    def test_stop_returns_while_the_check_is_parked(self):
        self.assertTrue(self.entered.wait(5))
        start = time.monotonic()
        self.manager.stop()
        self.assertLess(time.monotonic() - start, 5.0,
                        "stop() waited for the parked health check")
        self.assertIsNone(self.manager.health_check)

    def test_the_parked_check_is_the_last_one(self):
        self.assertTrue(self.entered.wait(5))
        self.manager.stop()
        self.release.set()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        seen = self.calls
        for _ in range(3):               # would have run several times over
            time.sleep(0.05)
            self.assertEqual(seen, self.calls)


class ADroppedServerIsAnnouncedTest(unittest.TestCase):
    """Every disconnect changes what the switcher offers, so each one says
    so -- over several drops, not one."""

    def test_each_disconnect_announces_the_change(self):
        manager = clients.ClientManager()
        seen = []
        manager.on_servers_changed = lambda: seen.append(1)
        for n in range(3):
            client = mock.Mock()
            server = {"uuid": "srv%d" % n}
            manager.clients[server["uuid"]] = client
            with mock.patch.object(clients.ClientManager, "_stop_client"):
                self.assertTrue(manager._disconnect_client(
                    server=server, expected_client=client))
            self.assertEqual(n + 1, len(seen))
            self.assertFalse(server["connected"])


if __name__ == "__main__":
    unittest.main()
