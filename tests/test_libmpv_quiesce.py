"""libmpv is shut down, and its "shutdown" seen, before the handle goes.

python-mpv's terminate() destroys the handle and only then joins its event
thread, so an observer still running read a freed handle -- a SIGSEGV in
mpv_get_property, 12 of 20 idle-quit re-opens on the Kubuntu VM. The order
here is the one python-mpv's own warning gives: quit, wait for "shutdown",
terminate.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import contextlib
import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim import player as player_module  # noqa: E402
from jellyfin_mpv_shim.player import PlayerManager  # noqa: E402


class _Handle:
    """Journals the teardown, with python-mpv's wait signature. "shutdown"
    usually resolves the wait normally; ``raced`` is the other exit, the
    event thread getting there first and check_core_alive() raising
    ShutdownError. ``wedged`` makes the wait time out. ``observer_raises``
    is an app observer raising while quit() winds the core down: with
    ``catch_errors`` python-mpv hands that exception to the wait."""

    def __init__(self, core_shutdown=False, wedged=False, raced=False,
                 observer_raises=False):
        self.journal = []
        self.core_shutdown = core_shutdown
        self.wedged = wedged
        self.raced = raced
        self.observer_raises = observer_raises

    @contextlib.contextmanager
    def prepare_and_wait_for_event(self, *events, cond=lambda evt: True,
                                   timeout=None, catch_errors=True):
        self.journal.append(("wait", events))
        yield
        if self.observer_raises and catch_errors:
            raise RuntimeError("an observer raised during quit")
        if self.wedged:
            raise TimeoutError()
        self.journal.append("shutdown seen")
        if self.raced:
            raise player_module.mpv.ShutdownError(
                "libmpv core has been shutdown")

    def quit(self):
        self.journal.append("quit")

    def terminate(self):
        self.journal.append("terminate")


class TerminateQuiescesFirstTest(unittest.TestCase):
    def pm(self, handle):
        pm = PlayerManager.__new__(PlayerManager)
        pm._player = handle
        pm._mpv_alive = True
        pm.on_mpv_terminated = None
        return pm

    def terminate(self, handle, ext=False):
        with mock.patch.object(player_module, "is_using_ext_mpv", ext):
            self.pm(handle)._terminate_mpv(handle)
        return handle.journal

    def test_the_core_is_down_before_the_handle_is_destroyed(self):
        journal = self.terminate(_Handle())
        self.assertEqual(
            [("wait", ("shutdown",)), "quit", "shutdown seen", "terminate"],
            journal)

    @unittest.skipUnless(hasattr(player_module.mpv, "ShutdownError"),
                         "libmpv backend not loaded")
    def test_shutdown_reached_by_the_event_thread_first(self):
        journal = self.terminate(_Handle(raced=True))
        self.assertEqual(
            [("wait", ("shutdown",)), "quit", "shutdown seen", "terminate"],
            journal)

    def test_an_observer_raising_during_quit_does_not_end_the_wait(self):
        journal = self.terminate(_Handle(observer_raises=True))
        self.assertEqual(
            [("wait", ("shutdown",)), "quit", "shutdown seen", "terminate"],
            journal)

    def test_a_core_already_down_is_not_asked_again(self):
        # The user closed the window: the event has been and gone, and a
        # wait for it would only sit out the timeout.
        self.assertEqual(["terminate"],
                         self.terminate(_Handle(core_shutdown=True)))

    def test_a_wedged_core_is_destroyed_anyway(self):
        journal = self.terminate(_Handle(wedged=True))
        self.assertEqual("terminate", journal[-1])
        self.assertIn("quit", journal)

    def test_external_mpv_is_terminated_as_before(self):
        # A process and a socket: there is no in-process handle to race.
        self.assertEqual(["terminate"], self.terminate(_Handle(), ext=True))


if __name__ == "__main__":
    unittest.main()
