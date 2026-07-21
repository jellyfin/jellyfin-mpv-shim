"""Closing the window must not park the action thread on a lost reply.

The bug: on the external-mpv (jsonipc) backend every command is a
request/response over a socket, and the reply is waited for with
``python_mpv_jsonipc.TIMEOUT`` — 120 seconds. Closing the window lets mpv
accept a command, run it, and exit before answering. The action thread
then sat for two minutes inside ``PlayerManager.stop()``, and because the
shutdown sequence joins that thread, the whole app hung with no window.

``bound_ipc_replies`` lowers that wait on the teardown paths. These tests
cover the tightening itself; the wait it bounds lives in the library.
"""

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


class WindowCloseTightensTheWaitTest(unittest.TestCase):
    """The close path has to tighten the wait *before* it queues the stop
    task, since that task is what issues the doomed command."""

    def test_handle_close_win_calls_it_before_queueing(self):
        import inspect
        from jellyfin_mpv_shim import player

        src = inspect.getsource(player.PlayerManager._init_mpv)
        start = src.index("def handle_close_win")
        body = src[start:start + 900]
        self.assertIn("bound_ipc_replies()", body,
                      "the close path no longer bounds the reply wait")
        self.assertLess(
            body.index("bound_ipc_replies()"), body.index("put_task"),
            "the wait must be bounded before the stop task is queued — "
            "that task is the one that issues the command whose reply "
            "never arrives")


if __name__ == "__main__":
    unittest.main()
