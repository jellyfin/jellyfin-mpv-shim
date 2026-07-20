"""The browser's background threads: single-start, and a shutdown event that
stops all of them.

Both properties used to be implicit. _start_np_ticker, _poll_downloads and
_poll_download_status each wrote "if the thread is None, start it" and then
assigned, which is check-then-act — and they are reachable from the loop
thread and from foreign ones (on_playstate, on_downloads_changed). Two
callers could both see None and both start a poller. The symptom is a
doubled refresh, which is exactly why it would never have been noticed.
"""

import sys
import threading
import time
import unittest

sys.argv = ["test"]      # the app parses argv on first config-dir resolution

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402


class _Source:
    def servers(self):
        return []


def _browser():
    b = MpvtkBrowser(app=None, source=_Source())
    b.controller = object()
    return b


class TestSingleStart(unittest.TestCase):
    def test_concurrent_starts_run_the_body_once(self):
        """Eight threads race into the same starter. Exactly one body runs."""
        b = _browser()
        runs = []
        gate = threading.Barrier(8)

        def body():
            runs.append(1)
            b._shutdown_evt.wait(30)

        def go():
            gate.wait()
            b._start_daemon("_np_thread", "t", body)

        workers = [threading.Thread(target=go) for _ in range(8)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(5)
        time.sleep(0.1)          # give any extra daemon time to run its body
        self.assertEqual(len(runs), 1,
                         "check-then-act let more than one daemon start")
        b.shutdown()

    def test_a_second_start_while_one_runs_is_a_no_op(self):
        b = _browser()
        runs = []
        started = threading.Event()

        def body():
            runs.append(1)
            started.set()
            b._shutdown_evt.wait(30)

        b._start_daemon("_np_thread", "t", body)
        self.assertTrue(started.wait(5))
        b._start_daemon("_np_thread", "t", body)
        time.sleep(0.05)
        self.assertEqual(len(runs), 1)
        b.shutdown()

    def test_the_slot_is_released_when_the_body_returns(self):
        b = _browser()
        done = threading.Event()
        b._start_daemon("_np_thread", "t", done.set)
        for _ in range(500):
            if b._np_thread is None:
                break
            time.sleep(0.005)
        self.assertTrue(done.is_set())
        self.assertIsNone(b._np_thread, "the slot was never released")
        b.shutdown()

    def test_an_exiting_daemon_does_not_unregister_its_successor(self):
        """The body may release its own slot early (the toast timer does, so
        the repaint it triggers can arm the next one). The exiting thread must
        then not null out whoever took its place."""
        b = _browser()
        first_done = threading.Event()

        def early():
            with b._poller_lock:
                b._np_thread = None      # release early, like the toast timer
            first_done.set()
            time.sleep(0.05)             # ... then linger

        b._start_daemon("_np_thread", "first", early)
        self.assertTrue(first_done.wait(5))
        b._start_daemon("_np_thread", "second", lambda: b._shutdown_evt.wait(30))
        successor = b._np_thread
        self.assertIsNotNone(successor)
        time.sleep(0.2)                  # let the first thread finish exiting
        self.assertIs(b._np_thread, successor,
                      "the departing thread cleared its successor's slot")
        b.shutdown()


class TestShutdownStopsEverything(unittest.TestCase):
    def test_every_daemon_waits_on_the_shared_event(self):
        """_shutdown_evt is set once, by shutdown(), and never cleared. All
        four background threads sleep on it, so setting it wakes them all."""
        b = _browser()
        woke = []

        def body(tag):
            def run():
                b._shutdown_evt.wait(30)
                woke.append(tag)
            return run

        for attr, tag in (("_np_thread", "np"), ("_dl_thread", "dl"),
                          ("_dlbar_thread", "bar"), ("_toast_timer", "toast")):
            b._start_daemon(attr, tag, body(tag))
        b.shutdown()
        for _ in range(500):
            if len(woke) == 4:
                break
            time.sleep(0.005)
        self.assertEqual(sorted(woke), ["bar", "dl", "np", "toast"],
                         "a background thread slept through shutdown")

    def test_shutdown_event_is_never_cleared(self):
        """It is the stop signal for four threads, not one poller's flag.
        Clearing it anywhere would silently resurrect them."""
        import ast
        import inspect
        import os
        from jellyfin_mpv_shim.mpvtk_browser import app as app_mod

        pkg = os.path.dirname(inspect.getfile(app_mod))
        offenders = []
        for mod in ("app", "settings", "music", "views", "tiles", "dialogs",
                    "auth", "queue_edit"):
            with open(os.path.join(pkg, mod + ".py")) as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "clear"
                        and isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr == "_shutdown_evt"):
                    offenders.append(f"{mod}:{node.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
