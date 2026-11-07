"""A group created mid-playback is handed what its creator is playing.

The server is meant to do this itself: ``Group.CreateGroup`` copies the
creator's ``session.NowPlayingQueue``. Since 12.0 (jellyfin/jellyfin
a3b2b9c07f) the server no longer records that queue from progress reports, so
the group arrives in ``Waiting`` with an empty queue and nobody who joins gets
anything to play. 10.11 still seeds it. The live half of this is
``tests/e2e/test_syncplay_create_while_playing.py``.
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
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.syncplay import SyncPlayManager  # noqa: E402
from tests._syncplay_server import FakeTimesync  # noqa: E402
from tests.test_syncplay_protocol import FakePlayer  # noqa: E402

GROUP = "group-1"


class FakeMedia:
    """`Media`'s queue as the player builds it: local PlaylistItemIds."""

    def __init__(self, ids, seq):
        self.queue = [{"PlaylistItemId": "playlistItem%d" % i, "Id": x}
                      for i, x in enumerate(ids)]
        self.seq = seq

    def replace_queue(self, sp_items, seq):
        return None


class FakeVideo:
    def __init__(self, parent):
        self.parent = parent


class Player(FakePlayer):
    def __init__(self, video=None, position=0.0):
        super().__init__()
        self.video = video
        self.position = position

    def get_video(self):
        return self.video


class Api:
    def __init__(self, fail_new=False):
        self.calls = []
        self.fail_new = fail_new

    def new_sync_play_v2(self, name):
        self.calls.append(("New", name))
        if self.fail_new:
            raise RuntimeError("400 Bad Request")

    def reset_queue_sync_play(self, ids, position=0, position_ticks=0):
        self.calls.append(("SetNewQueue", list(ids), position, position_ticks))

    def pause_sync_play(self):
        self.calls.append(("Pause",))

    def ready_sync_play(self, *a):
        self.calls.append(("Ready",) + a)

    def buffering_sync_play(self, *a):
        self.calls.append(("Buffering",) + a)

    def leave_sync_play(self):
        self.calls.append(("Leave",))

    def sent_queues(self):
        return [c for c in self.calls if c[0] == "SetNewQueue"]


class Client:
    def __init__(self, api):
        self.jellyfin = api
        self.timesync = FakeTimesync()


def joined():
    return {"Type": "GroupJoined", "GroupId": GROUP, "Data": {}}


def play_queue(items=(), reason="NewPlaylist", index=-1):
    return {"Type": "PlayQueue", "GroupId": GROUP, "Data": {
        "Reason": reason,
        "Playlist": [{"ItemId": x, "PlaylistItemId": "srv-%d" % i}
                     for i, x in enumerate(items)],
        "PlayingItemIndex": index,
        "StartPositionTicks": 0,
        "LastUpdate": None,
    }}


class SeedOnCreateTest(unittest.TestCase):
    def setUp(self):
        self.media = FakeMedia(["ep-1", "ep-2", "ep-3"], seq=1)
        self.player = Player(FakeVideo(self.media), position=42.5)
        self.api = Api()
        self.client = Client(self.api)
        self.sp = SyncPlayManager(self.player)
        self.sp.client = self.client
        patch = mock.patch("jellyfin_mpv_shim.syncplay.default_group_name",
                           lambda client: "alice's group")
        patch.start()
        self.addCleanup(patch.stop)

    def create(self):
        self.sp.create_group(self.client)
        # What the server sends the creator, in order: Group.CreateGroup's
        # GroupJoined, then WaitingGroupState.SessionJoined's PlayQueue.
        self.sp.process_group_update(joined())

    def test_an_unseeded_group_is_sent_what_we_are_playing(self):
        self.create()
        self.sp.process_group_update(play_queue())
        self.assertEqual(
            [("SetNewQueue", ["ep-1", "ep-2", "ep-3"], 1, 425_000_000)],
            self.api.sent_queues(),
            "a group created mid-playback was left with an empty queue")

    def test_a_paused_creator_gets_a_paused_group(self):
        """10.11 seeds with ResumePlaying = !IsPaused; SetNewQueue always
        resumes, so the Pause has to follow it, in that order."""
        self.player.paused = True
        self.create()
        self.sp.process_group_update(play_queue())
        kinds = [c[0] for c in self.api.calls]
        self.assertIn("SetNewQueue", kinds)
        self.assertIn("Pause", kinds, "the group was started for a creator "
                      "who had paused")
        self.assertLess(kinds.index("SetNewQueue"), kinds.index("Pause"))

    def test_a_playing_creator_is_not_paused(self):
        self.create()
        self.sp.process_group_update(play_queue())
        self.assertNotIn("Pause", [c[0] for c in self.api.calls])

    def test_a_seeded_group_is_left_alone(self):
        """10.11: the server already copied our queue. Sending it again
        would restart the group for nothing."""
        self.create()
        self.sp.process_group_update(
            play_queue(["ep-1", "ep-2", "ep-3"], index=1))
        self.assertEqual([], self.api.sent_queues())

    def test_joining_someone_elses_empty_group_does_not_take_it_over(self):
        self.sp.process_group_update(joined())
        self.sp.process_group_update(play_queue())
        self.assertEqual([], self.api.sent_queues(),
                         "joining is 'take me to what they watch', not "
                         "'make them watch mine'")

    def test_creating_with_nothing_playing_sends_nothing(self):
        self.player.video = None
        self.create()
        self.sp.process_group_update(play_queue())
        self.assertEqual([], self.api.sent_queues())

    def test_only_the_first_queue_after_creating_counts(self):
        """Over several updates, not one: a later empty queue in the same
        group (everything removed from it) is the group's decision."""
        self.create()
        for _ in range(3):
            self.sp.process_group_update(play_queue())
        self.assertEqual(1, len(self.api.sent_queues()))

    def test_a_failed_create_does_not_arm_a_later_join(self):
        self.api.fail_new = True
        with self.assertRaises(RuntimeError):
            self.sp.create_group(self.client)
        self.sp.process_group_update(joined())
        self.sp.process_group_update(play_queue())
        self.assertEqual([], self.api.sent_queues())

    def test_leaving_before_the_queue_arrives_disarms_it(self):
        self.create()
        self.sp.process_group_update(
            {"Type": "GroupLeft", "GroupId": GROUP, "Data": {}})
        self.sp.process_group_update(joined())
        self.sp.process_group_update(play_queue())
        self.assertEqual([], self.api.sent_queues())


if __name__ == "__main__":
    unittest.main()
