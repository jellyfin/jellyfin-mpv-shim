"""Joining a group re-asks whether the local copy may still be used.

The factory decides once, when playback starts. A group can be joined while a
film is already playing, and the size check that a group needs did not apply
then -- so the earlier answer can be wrong without anything about the video
changing.

The rule itself is tested in `tests/test_offline_media.py`; this module is
about the swap: that it is queued rather than run inline, that it re-resolves
the item remotely rather than replaying the same object, that it leaves the
owning `Media` consistent, and that it never takes the group down.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import textwrap
import unittest
from unittest import mock

# Importing syncplay must stay side-effect safe: it must not pull in player.py,
# which opens a real mpv window at import time.
from jellyfin_mpv_shim import media
from jellyfin_mpv_shim.syncplay import SyncPlayManager
from jellyfin_mpv_shim.sync import offline_media


class FakeMedia:
    """The owning `Media`. `video` is the attribute that goes stale."""

    def __init__(self, video=None):
        self.video = video
        self.client = object()


class FakeLocalVideo:
    def __init__(self, parent):
        self.item_id = "film"
        self.parent = parent


class FakePlayer:
    """Only what the swap reaches for. Deliberately not a subset: a stand-in
    missing one of these would make the path raise where nobody is looking.
    """

    def __init__(self, video=None, position=123.5):
        self.video = video
        self.position = position
        self.played = []
        self.tasks = []

    def get_video(self):
        return self.video

    def get_time(self):
        return self.position

    def put_task(self, func, *args):
        # Recorded rather than run, so a test can assert the work was QUEUED.
        # The real one hands it to the action thread; running it inline here
        # would make "queued" and "called" indistinguishable.
        self.tasks.append((func, args))

    def play(self, video, offset=None, **kwargs):
        self.played.append((video, offset, kwargs))
        self.video = video


def _manager(player):
    sp = SyncPlayManager.__new__(SyncPlayManager)
    sp.playerManager = player
    return sp


class MidFilmGroupJoinTest(unittest.TestCase):
    def setUp(self):
        self.parent = FakeMedia()
        self.local = FakeLocalVideo(self.parent)
        self.parent.video = self.local
        self.player = FakePlayer(video=self.local)
        self.sp = _manager(self.player)
        self.remote = object()
        patch = mock.patch.object(media, "Video",
                                  lambda item_id, parent: self.remote)
        patch.start()
        self.addCleanup(patch.stop)

    def _rule(self, still_ok):
        patch = mock.patch.object(offline_media, "still_substitutable",
                                  lambda video: still_ok)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_stale_local_copy_is_swapped_for_the_server_at_the_position(self):
        self._rule(False)
        self.sp._drop_a_local_copy_a_group_cannot_use()
        self.assertEqual(1, len(self.player.played),
                         "the server's copy was never played")
        video, offset, _kwargs = self.player.played[0]
        self.assertIs(self.remote, video,
                      "the same video object was replayed; that cannot change "
                      "where the media comes from")
        self.assertEqual(123.5, offset, "the position was not carried over")

    def test_the_owning_media_stops_pointing_at_the_dropped_video(self):
        """`Media.get_video(0)` hands back its cached `video`, and player.py
        reads `media.video` for the group skip -- so a stale one keeps a
        torn-down local video alive in two places."""
        self._rule(False)
        self.sp._drop_a_local_copy_a_group_cannot_use()
        self.assertIs(self.remote, self.parent.video)

    def test_a_media_pointing_somewhere_else_is_left_alone(self):
        """Identity-checked: only the video actually being replaced."""
        self._rule(False)
        other = object()
        self.parent.video = other
        self.sp._drop_a_local_copy_a_group_cannot_use()
        self.assertIs(other, self.parent.video,
                      "clobbered a Media whose video was not the one swapped")

    def test_a_copy_that_still_stands_is_not_disturbed(self):
        self._rule(True)
        self.sp._drop_a_local_copy_a_group_cannot_use()
        self.assertEqual([], self.player.played)
        self.assertIs(self.local, self.parent.video)

    def test_nothing_playing_is_not_an_error(self):
        self._rule(False)
        self.player.video = None
        self.sp._drop_a_local_copy_a_group_cannot_use()
        self.assertEqual([], self.player.played)

    def test_a_failure_does_not_take_the_group_down(self):
        """Worst case the local copy keeps playing, which is what happened
        before this existed. A group must not die over it."""
        self._rule(False)

        def boom(video, offset=None, **kwargs):
            raise RuntimeError("playback start failed")

        self.player.play = boom
        self.sp._drop_a_local_copy_a_group_cannot_use()   # must not raise

class EnableQueuesTheCheckTest(unittest.TestCase):
    """`enable_sync_play` must QUEUE the swap, not call it.

    Asserted against the source rather than by driving `enable_sync_play`,
    which needs a timesync, a client and a websocket to reach its end. Driving
    it with all of that faked would prove the fakes ran; reading the call tells
    us which of two shapes was written, and that is the actual claim. The
    earlier version of this test called `put_task` itself and then asserted the
    fake had recorded it -- a test that could not fail for its stated reason.
    """

    def _enable_body(self):
        import ast
        import inspect

        src = inspect.getsource(SyncPlayManager.enable_sync_play)
        return ast.parse(textwrap.dedent(src))

    def test_the_swap_is_handed_to_put_task(self):
        import ast

        queued = []
        for node in ast.walk(self._enable_body()):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "put_task":
                queued += [a.attr for a in node.args
                           if isinstance(a, ast.Attribute)]
        self.assertIn(
            "_drop_a_local_copy_a_group_cannot_use", queued,
            "enable_sync_play does not queue the local-copy check; called "
            "inline it would make a network round trip and start playback on "
            "the websocket event thread")

    def test_it_is_queued_after_enabled_at_is_set(self):
        """Ordering, not presence: the rule asks `in_group()`, which reads
        `enabled_at`. Queued before it is set, the group is invisible and the
        swap silently never happens."""
        import ast

        body = self._enable_body()
        set_at = queue_at = None
        for node in ast.walk(body):
            if (isinstance(node, ast.Attribute) and node.attr == "enabled_at"
                    and isinstance(node.ctx, ast.Store)):
                set_at = node.lineno if set_at is None else set_at
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "put_task"):
                queue_at = node.lineno
        self.assertIsNotNone(set_at, "enabled_at is no longer set here")
        self.assertIsNotNone(queue_at, "the swap is no longer queued here")
        self.assertLess(set_at, queue_at,
                        "the swap is queued before enabled_at is set, so "
                        "in_group() answers False and it never fires")


if __name__ == "__main__":
    unittest.main()
