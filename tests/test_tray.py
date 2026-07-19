"""TrayManager command dispatch.

The pystray loop needs a real desktop, and it lives in a separate process
anyway (it needs its process's main thread, and pystray + libmpv in one
process segfaults with GNOME AppIndicator). What's testable here is the
parent side: how commands from that child are dispatched.
"""

import multiprocessing
import threading
import unittest

from jellyfin_mpv_shim.tray import TrayManager


class TestTrayDispatch(unittest.TestCase):
    def test_dispatches_known_commands(self):
        seen = []
        m = TrayManager({"show": lambda: seen.append("show"),
                         "quit": lambda: seen.append("quit")})
        m.dispatch("show")
        m.dispatch("quit")
        self.assertEqual(seen, ["show", "quit"])

    def test_unknown_command_is_ignored(self):
        TrayManager({}).dispatch("does_not_exist")   # must not raise

    def test_handler_exception_does_not_propagate(self):
        def boom():
            raise RuntimeError("nope")

        m = TrayManager({"show": boom})
        m.dispatch("show")          # swallowed, so the pump survives

    def test_ready_marks_the_tray_available(self):
        m = TrayManager({})
        m.dispatch("ready")
        self.assertTrue(m.available)
        self.assertTrue(m.ready.is_set())

    def test_tray_died_is_not_available_but_still_unblocks(self):
        m = TrayManager({})
        m.dispatch("tray_died")
        self.assertFalse(m.available)
        # ready is set either way, so nothing waiting on the tray can hang
        # when pystray/AppIndicator is missing.
        self.assertTrue(m.ready.is_set())

    def test_stop_without_start_is_safe(self):
        TrayManager({}).stop()


class TestTrayPump(unittest.TestCase):
    def test_pump_drains_the_queue_and_honours_halt(self):
        seen = threading.Event()
        m = TrayManager({"show": seen.set})
        m._queue = multiprocessing.Queue()
        thread = threading.Thread(target=m._pump, daemon=True)
        thread.start()
        try:
            m._queue.put(("show", None))
            self.assertTrue(seen.wait(3), "pump did not dispatch")
        finally:
            m._halt.set()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
