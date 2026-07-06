import os
import tempfile
import threading
import unittest
from unittest import mock

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

    def test_lock_files_are_private(self):
        if os.name != "posix":
            self.skipTest("permission bits are POSIX-specific")
        a = self._make()
        self.assertTrue(a.acquire())
        self.assertEqual(os.stat(a._guardpath).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(a._lockpath).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
