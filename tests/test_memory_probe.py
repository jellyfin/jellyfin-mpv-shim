"""How much RAM there is, and whether that is a reason to work differently.

The browser trades speed for memory in one place — it drops the composited
rows of the screen you just left, which is what makes going *back* instant —
and that trade is only worth making on a machine that is actually short. So
the probe decides a behaviour, and being wrong in the pessimistic direction
costs every navigation on a machine with 128 GiB of RAM.

Read out of the system rather than depended on: psutil answers all of this
in one line and is a compiled dependency to ask how much memory a machine
has.
"""

import unittest
from unittest import mock

from jellyfin_mpv_shim import utils


SAMPLE = """MemTotal:        7940000 kB
MemFree:          210000 kB
MemAvailable:    3100000 kB
Buffers:          100000 kB
Cached:          4000000 kB
"""


class MeminfoTest(unittest.TestCase):
    def test_it_reads_total_and_available(self):
        with mock.patch("builtins.open", mock.mock_open(read_data=SAMPLE)):
            total, avail = utils._meminfo()
        self.assertEqual(total, 7940000 * 1024)
        self.assertEqual(avail, 3100000 * 1024)

    def test_it_reads_available_and_not_free(self):
        """MemFree excludes reclaimable page cache, so a healthy Linux box
        that has merely read some files looks nearly out of memory. Using it
        would put every machine that has been up a while into the degraded
        path. MemAvailable is the kernel's own estimate of what a new
        allocation could actually get."""
        with mock.patch("builtins.open", mock.mock_open(read_data=SAMPLE)):
            _total, avail = utils._meminfo()
        self.assertNotEqual(avail, 210000 * 1024, "read MemFree, not MemAvailable")

    def test_an_unreadable_file_is_not_an_answer(self):
        with mock.patch("builtins.open", side_effect=OSError):
            self.assertEqual(utils._meminfo(), (None, None))

    def test_a_garbled_file_is_not_an_answer(self):
        with mock.patch("builtins.open",
                        mock.mock_open(read_data="MemTotal: banana\n")):
            self.assertEqual(utils._meminfo(), (None, None))


class MemoryIsTightTest(unittest.TestCase):
    GB = 1024 ** 3

    def test_a_small_machine_is_tight_even_when_idle(self):
        # 4 GiB with 3 free is not under pressure this second, but it has no
        # headroom to be wrong about.
        self.assertTrue(utils.memory_is_tight(4 * self.GB, 3 * self.GB))

    def test_a_big_machine_under_load_is_tight_too(self):
        # ...and 64 GiB with 1 free is not small, but something else needs
        # the room now.
        self.assertTrue(utils.memory_is_tight(64 * self.GB, 1 * self.GB))

    def test_a_roomy_machine_is_not(self):
        self.assertFalse(utils.memory_is_tight(32 * self.GB, 20 * self.GB))

    def test_the_boundaries_are_where_they_are_documented(self):
        self.assertFalse(utils.memory_is_tight(8 * self.GB, 8 * self.GB))
        self.assertTrue(utils.memory_is_tight(8 * self.GB - 1, 8 * self.GB))
        self.assertFalse(utils.memory_is_tight(16 * self.GB, 2 * self.GB))
        self.assertTrue(utils.memory_is_tight(16 * self.GB, 2 * self.GB - 1))

    def test_unknown_is_roomy_not_tight(self):
        """A probe that cannot answer has no business degrading the app on a
        machine that may be perfectly comfortable. macOS reaches this for
        `available` — there is no cheap portable source for it — and answers
        on the total alone."""
        self.assertFalse(utils.memory_is_tight(None, None))
        self.assertFalse(utils.memory_is_tight(64 * self.GB, None))
        self.assertTrue(utils.memory_is_tight(2 * self.GB, None),
                        "a small machine is small whether or not it is busy")

    def test_the_real_probe_answers_something_usable(self):
        total, avail = utils.system_memory()
        # Total is answerable on every platform the suite runs on; available
        # is not, and None is a legitimate answer.
        self.assertTrue(total is None or total > 0)
        self.assertTrue(avail is None or avail >= 0)
        self.assertIn(utils.memory_is_tight(), (True, False))


if __name__ == "__main__":
    unittest.main()
