"""Closing the window must not park the action thread on a lost reply.

The bug: on the external-mpv (jsonipc) backend every command is a
request/response over a socket, and the reply is waited for with
``python_mpv_jsonipc.TIMEOUT`` — 120 seconds. Closing the window lets mpv
accept a command, run it, and exit before answering. The action thread
then sat for two minutes inside ``PlayerManager.stop()``, and because the
shutdown sequence joins that thread, the whole app hung with no window.

python-mpv-jsonipc 1.5.0 fails such a wait the moment the connection
closes. ``bound_ipc_replies`` still lowers it on the quit paths -- and only
there, never on the window close (see the last class). These tests cover
the tightening itself; the wait it bounds lives in the library.
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
import types
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()


class BoundIpcRepliesTest(unittest.TestCase):
    def setUp(self):
        from jellyfin_mpv_shim import player

        self.player = player
        self.fake_mpv = types.SimpleNamespace(TIMEOUT=120)
        # player.mpv is the backend module under either alias; patching it
        # keeps the test off whichever backend happens to be installed.
        self.patches = [
            mock.patch.object(self.player, "mpv", self.fake_mpv),
            mock.patch.object(self.player, "is_using_ext_mpv", True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_it_lowers_the_reply_wait(self):
        self.player.bound_ipc_replies()
        self.assertEqual(self.fake_mpv.TIMEOUT,
                         self.player.IPC_TEARDOWN_TIMEOUT)

    def test_the_bound_is_short_enough_to_matter(self):
        # The join that waits on the action thread gives it 15s; a reply
        # wait at or above that reproduces the original hang exactly.
        from jellyfin_mpv_shim.action_thread import ActionThread

        self.assertLess(self.player.IPC_TEARDOWN_TIMEOUT,
                        ActionThread.JOIN_TIMEOUT,
                        "a teardown command could still outlast the join "
                        "that waits for the thread running it")

    def test_it_never_raises_the_wait(self):
        # Called from several teardown paths; a later call must not undo
        # an earlier, tighter one.
        self.fake_mpv.TIMEOUT = 1
        self.player.bound_ipc_replies()
        self.assertEqual(self.fake_mpv.TIMEOUT, 1)

    def test_it_is_idempotent(self):
        self.player.bound_ipc_replies()
        self.player.bound_ipc_replies()
        self.assertEqual(self.fake_mpv.TIMEOUT,
                         self.player.IPC_TEARDOWN_TIMEOUT)

    def test_libmpv_is_left_alone(self):
        # libmpv raises immediately on a dead handle instead of waiting for
        # a reply, so it never had this bug and has no such knob.
        with mock.patch.object(self.player, "is_using_ext_mpv", False):
            self.player.bound_ipc_replies()
        self.assertEqual(self.fake_mpv.TIMEOUT, 120)

    def test_a_backend_without_the_knob_does_not_break_teardown(self):
        # Defensive: this runs on the close path, where raising would
        # replace a slow shutdown with a broken one.
        with mock.patch.object(self.player, "mpv", object()):
            self.player.bound_ipc_replies()      # must not raise


class WindowCloseLeavesTheWaitAloneTest(unittest.TestCase):
    """Closing the window is not a teardown: with a tray it only hides the
    window, and the same mpv keeps serving the session. Bounding the wait
    there left every later command of that mpv timing out at 5 s, which
    reads as a disconnect (`_mpv_errors`). The reply mpv never sends because
    it exited is python-mpv-jsonipc's to fail since 1.5.0, which answers
    every waiting command with BrokenPipeError when the connection closes.
    """

    def _closer(self, **attrs):
        """A PlayerManager with no mpv attached; __new__ skips _init_mpv."""
        from jellyfin_mpv_shim.player import PlayerManager

        pm = PlayerManager.__new__(PlayerManager)
        pm.mpvtk_active = False
        pm.on_window_closed = None
        for k, v in attrs.items():
            setattr(pm, k, v)
        return pm

    def _close(self, pm):
        from jellyfin_mpv_shim import player

        calls = []
        pm.put_task = lambda fn, *a: calls.append(("put_task", fn))
        with mock.patch.object(
                player, "bound_ipc_replies",
                lambda: calls.append(("bound_ipc_replies", None))):
            pm._on_close_win()
        return calls

    def test_minimizing_to_the_tray_does_not_bound_it(self):
        handler = lambda: None      # noqa: E731
        calls = self._close(self._closer(mpvtk_active=True,
                                         on_window_closed=handler))
        self.assertEqual(calls, [("put_task", handler)],
                         "closing the window with the in-window UI up must "
                         "hand over to it and leave the reply wait alone")

    def test_closing_without_the_ui_stops_and_leaves_it_to_the_quit(self):
        pm = self._closer()
        calls = self._close(pm)
        self.assertEqual(calls, [("put_task", pm.stop_and_close)])


if __name__ == "__main__":
    unittest.main()
