import os
import socket
import tempfile
import threading
import unittest
from unittest import mock

from jellyfin_mpv_shim import single_instance
from jellyfin_mpv_shim.single_instance import SingleInstance


def make_instance(tmpdir):
    # conffile.get parses --config from argv (which unittest pollutes) and
    # resolves the real config dir; keep tests hermetic.
    with mock.patch(
        "jellyfin_mpv_shim.single_instance.conffile.get",
        return_value=os.path.join(tmpdir, "instance.lock"),
    ):
        return SingleInstance()


class SingleInstanceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.instances = []
        self.addCleanup(lambda: [i.release() for i in self.instances])

    def _make(self):
        inst = make_instance(self._tmp.name)
        self.instances.append(inst)
        return inst

    def test_first_instance_becomes_primary(self):
        a = self._make()
        self.assertTrue(a.acquire())
        self.assertIsNotNone(a._guard_fd)

    def test_second_instance_blocked_and_primary_activated(self):
        a = self._make()
        activated = threading.Event()
        self.assertTrue(a.acquire())
        a.on_activate = activated.set

        b = self._make()
        self.assertFalse(b.acquire())
        self.assertTrue(activated.wait(5), "primary was not asked to activate")

    def test_release_allows_new_primary(self):
        a = self._make()
        self.assertTrue(a.acquire())
        a.release()

        c = self._make()
        self.assertTrue(c.acquire())

    def test_unresponsive_primary_still_blocks_duplicates(self):
        # The election is the lock, not the handoff: even when the primary's
        # listener is gone (simulated by closing its socket), a second launch
        # must refuse to run rather than become a duplicate catalog writer.
        a = self._make()
        self.assertTrue(a.acquire())
        a._sock.close()

        b = self._make()
        self.assertFalse(b.acquire())

    def test_stop_request_reaches_the_primary(self):
        a = self._make()
        stopped = threading.Event()
        activated = threading.Event()
        self.assertTrue(a.acquire())
        a.on_stop = stopped.set
        a.on_activate = activated.set

        b = self._make()
        self.assertTrue(b.request_stop())
        self.assertTrue(stopped.wait(5), "primary was not asked to stop")
        # A stop must never be mistaken for the activation it shares a channel
        # with -- that would surface the window instead of closing the app.
        self.assertFalse(activated.is_set())

    def test_stop_request_without_a_primary_reports_failure(self):
        a = self._make()
        self.assertFalse(a.request_stop())
        self.assertFalse(a.is_running())

    def test_stop_request_to_a_wedged_primary_reports_it_running(self):
        # The pair the CLI uses to tell "nothing to stop" from "something is
        # there but will not answer", which are different exit codes.
        a = self._make()
        self.assertTrue(a.acquire())
        # Repoint the endpoint at a port nothing listens on. Closing a._sock
        # would not do it: on Linux the blocked accept() in the listener
        # thread keeps the socket serving until it returns.
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()
        a._write_endpoint(dead_port)

        b = self._make()
        self.assertFalse(b.request_stop())
        self.assertTrue(b.is_running())

    def test_is_running_does_not_keep_the_lock(self):
        a = self._make()
        self.assertFalse(a.is_running())
        b = self._make()
        self.assertTrue(b.acquire(), "is_running() left the guard lock held")

    def test_activation_wire_format_is_unchanged(self):
        # A newer client must still be able to activate an older primary, and
        # that one compares the entire payload against its token -- so SHOW
        # may not gain a command word.
        a = self._make()
        self.assertTrue(a.acquire())
        sent = []
        a.on_activate = lambda: None

        b = self._make()
        real_create = single_instance.socket.create_connection

        class Recorder:
            def __init__(self, conn):
                self._conn = conn

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._conn.__exit__(*exc)

            def sendall(self, data):
                sent.append(data)
                return self._conn.sendall(data)

            def recv(self, n):
                return self._conn.recv(n)

        with mock.patch.object(
            single_instance.socket,
            "create_connection",
            lambda *a_, **k: Recorder(real_create(*a_, **k)),
        ):
            self.assertFalse(b.acquire())
        self.assertEqual(sent, [b"JMS1" + a._token + b"\n"])

    def test_lock_files_are_private(self):
        if os.name != "posix":
            self.skipTest("permission bits are POSIX-specific")
        a = self._make()
        self.assertTrue(a.acquire())
        self.assertEqual(os.stat(a._guardpath).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(a._lockpath).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
