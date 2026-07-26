"""Startup connect: parallel across servers, serial within one server.

Connecting was serial over every saved credential, and authenticate carries a
10s+ timeout — so one server being down delayed the whole library by that much
before anything rendered. Now each server gets its own thread, and the UI
renders as soon as the server it wants to open on is up.
"""

import sys
import threading
import time
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

import jellyfin_mpv_shim.clients as clients  # noqa: E402


def cred(server_id, address, uuid=None):
    return {"Id": server_id, "address": address,
            "uuid": uuid or ("%s-%s" % (server_id, address))}


class ConnectHarness(unittest.TestCase):
    def setUp(self):
        self.mgr = clients.ClientManager.__new__(clients.ClientManager)
        # is_stopping is derived from _stop_event; drive the real thing.
        self.mgr._stop_event = threading.Event()
        self.mgr.credentials = []
        self.notified = []
        self.mgr.on_server_connected = lambda: self.notified.append(True)

        self.attempts = []
        self._attempt_lock = threading.Lock()
        self.behaviour = {}       # uuid -> (delay_secs, succeeds)
        self.mgr.connect_client = self._connect_client

        # Deterministic priority: these are not real addresses.
        self._orig_last = clients.userManager.get_last_server
        self.last_server = None
        clients.userManager.get_last_server = lambda: self.last_server
        self.addCleanup(
            lambda: setattr(clients.userManager, "get_last_server",
                            self._orig_last))

    def _connect_client(self, server, do_retries=True):
        delay, ok = self.behaviour.get(server["uuid"], (0, True))
        with self._attempt_lock:
            self.attempts.append((server["uuid"], time.time()))
        if delay:
            time.sleep(delay)
        return ok

    def _uuids(self):
        with self._attempt_lock:
            return [u for u, _t in self.attempts]


class ParallelAcrossServersTest(ConnectHarness):
    def test_a_down_server_does_not_delay_the_others(self):
        """The reported symptom: a friend's server being down held up the
        whole library for the length of its timeout."""
        self.mgr.credentials = [cred("down", "a"), cred("up", "b")]
        self.behaviour["down-a"] = (1.0, False)

        started = time.time()
        self.assertTrue(self.mgr._connect_all())
        elapsed = time.time() - started

        self.assertLess(elapsed, 0.9,
                        "the working server waited on the dead one")

    def test_every_server_is_attempted(self):
        self.mgr.credentials = [cred("a", "1"), cred("b", "2"),
                                cred("c", "3")]
        self.mgr._connect_all()
        self.assertEqual(set(self._uuids()), {"a-1", "b-2", "c-3"})

    def test_reports_failure_when_nothing_connects(self):
        self.mgr.credentials = [cred("a", "1"), cred("b", "2")]
        self.behaviour = {"a-1": (0, False), "b-2": (0, False)}
        self.assertFalse(self.mgr._connect_all())

    def test_no_credentials_is_not_a_connection(self):
        self.assertFalse(self.mgr._connect_all())


class SerialWithinOneServerTest(ConnectHarness):
    """Addresses for ONE server stay a fallback chain: the priority sort puts
    the most local address first, and racing them would let a worse route
    win."""

    def test_the_second_address_is_only_tried_if_the_first_fails(self):
        self.mgr.credentials = [cred("same", "local"), cred("same", "remote")]
        self.behaviour["same-local"] = (0, False)
        self.assertTrue(self.mgr._connect_all())
        self.assertEqual(self._uuids(), ["same-local", "same-remote"])

    def test_a_working_first_address_stops_the_chain(self):
        self.mgr.credentials = [cred("same", "local"), cred("same", "remote")]
        self.mgr._connect_all()
        self.assertEqual(self._uuids(), ["same-local"],
                         "the fallback address was contacted needlessly")


class PreferredServerTest(ConnectHarness):
    def test_the_remembered_server_releases_the_ui_early(self):
        """The whole point: render on the server the user opens on, and let
        the rest arrive behind the homepage."""
        self.mgr.credentials = [cred("slow", "a"), cred("mine", "b")]
        self.behaviour["slow-a"] = (1.0, True)
        self.last_server = "mine-b"

        started = time.time()
        self.assertTrue(self.mgr._connect_all())
        self.assertLess(time.time() - started, 0.9,
                        "rendering waited for an unrelated server")

    def test_a_late_server_is_announced_so_it_appears(self):
        """It connected after the UI rendered without it, so the browser has
        to be told to rebuild — otherwise it stays invisible until restart."""
        self.mgr.credentials = [cred("slow", "a"), cred("mine", "b")]
        self.behaviour["slow-a"] = (0.2, True)
        self.last_server = "mine-b"

        self.mgr._connect_all()
        deadline = time.time() + 5
        while not self.notified and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.notified,
                        "the late server never reached the browser")

    def test_a_server_up_before_the_render_is_not_announced(self):
        """Nothing rendered without it, so there is nothing to rebuild."""
        self.mgr.credentials = [cred("mine", "b")]
        self.last_server = "mine-b"
        self.mgr._connect_all()
        time.sleep(0.05)
        self.assertEqual(self.notified, [])

    def test_an_unreachable_preferred_server_still_waits_for_the_rest(self):
        """Otherwise we would render 'no servers' and then pop the library in
        a moment later."""
        self.mgr.credentials = [cred("mine", "a"), cred("other", "b")]
        self.behaviour["mine-a"] = (0, False)
        self.behaviour["other-b"] = (0.2, True)
        self.last_server = "mine-a"

        self.assertTrue(self.mgr._connect_all(),
                        "gave up before the reachable server answered")

    def test_no_remembered_server_falls_back_to_the_first(self):
        self.mgr.credentials = [cred("a", "1"), cred("b", "2")]
        self.last_server = None
        self.assertTrue(self.mgr._connect_all())

    def test_a_remembered_server_that_is_gone_is_tolerated(self):
        self.mgr.credentials = [cred("a", "1")]
        self.last_server = "deleted-uuid"
        self.assertTrue(self.mgr._connect_all())

    def test_a_failing_last_server_lookup_does_not_break_connecting(self):
        def boom():
            raise RuntimeError("users.json unreadable")

        clients.userManager.get_last_server = boom
        self.mgr.credentials = [cred("a", "1")]
        self.assertTrue(self.mgr._connect_all())


class ShutdownTest(ConnectHarness):
    def test_stopping_halts_the_chain(self):
        self.mgr.credentials = [cred("same", "a"), cred("same", "b")]
        self.mgr._stop_event.set()
        self.assertFalse(self.mgr._connect_all())
        self.assertEqual(self._uuids(), [])

    def test_a_raising_connect_does_not_wedge_the_wait(self):
        """A chain that throws must still count as finished, or _connect_all
        blocks forever on a server that errored."""
        def explode(server, do_retries=True):
            raise RuntimeError("boom")

        self.mgr.connect_client = explode
        self.mgr.credentials = [cred("a", "1")]

        finished = threading.Event()

        def run():
            self.mgr._connect_all()
            finished.set()

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(finished.wait(5),
                        "a raising connect left the startup wait hanging")


if __name__ == "__main__":
    unittest.main()
