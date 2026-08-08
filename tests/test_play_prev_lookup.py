"""Stepping back past the start of the queue (#650).

Starting an episode from Next Up or Continue Watching builds the queue with
``StartItemId``, which is *inclusive* — so the queue is that episode
onward, ``has_prev`` is False, and the previous button does nothing.
jellyfin-web has the same gap; the issue asks for "load more/full list, so
going back is possible".

The property worth pinning is the multi-step one, per this repo's standing
rule: press previous three times and you should walk back three episodes,
with one server round trip in total. A one-step test passes against an
implementation that can only ever go back one.
"""

import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.media import Media  # noqa: E402


EPISODES = ["e1", "e2", "e3", "e4", "e5"]


class FakeJellyfin:
    def __init__(self, episodes=EPISODES):
        self.episodes = episodes
        self.calls = 0

    def get_episodes(self, series_id, **kw):
        self.calls += 1
        return {"Items": [{"Id": e} for e in self.episodes]}


def _player(playing="e4", queue=None, item_type="Episode",
            series="s1", client=True, syncplay=False, episodes=EPISODES):
    """A PlayerManager with only the collaborators this path touches."""
    from jellyfin_mpv_shim.player import PlayerManager

    pm = PlayerManager.__new__(PlayerManager)
    pm.syncplay = mock.Mock()
    pm.syncplay.is_enabled.return_value = syncplay
    api = FakeJellyfin(episodes)
    video = mock.Mock()
    video.item_id = playing
    video.item = {"Type": item_type, "SeriesId": series}
    video.client = mock.Mock(jellyfin=api) if client else None
    ids = queue if queue is not None else [playing]
    media = Media.__new__(Media)
    media.queue = [{"PlaylistItemId": "p%d" % i, "Id": q}
                   for i, q in enumerate(ids)]
    media.seq = ids.index(playing)
    media.has_next = media.seq < len(ids) - 1
    media.has_prev = media.seq > 0
    video.parent = media
    pm._video = video
    return pm, video, media, api


class WidenBackwardsTest(unittest.TestCase):

    def test_a_next_up_start_gains_everything_before_it(self):
        pm, _v, media, api = _player(playing="e4")
        self.assertTrue(pm._widen_queue_backwards(pm._video))
        self.assertEqual([q["Id"] for q in media.queue], EPISODES[:4])
        self.assertEqual(media.seq, 3)
        self.assertTrue(media.has_prev)
        self.assertEqual(api.calls, 1)

    def test_it_keeps_what_was_already_after_us(self):
        """The entries ahead already exist, carry their PlaylistItemIds and
        may have been edited from the queue screen. Rebuilding the whole
        queue from the server's episode list would discard that."""
        pm, _v, media, _api = _player(playing="e4", queue=["e4", "e5"])
        ahead = [dict(q) for q in media.queue]
        pm._widen_queue_backwards(pm._video)
        self.assertEqual([q["Id"] for q in media.queue], EPISODES)
        self.assertEqual(media.queue[3:], ahead)

    def test_walking_back_three_times_asks_the_server_once(self):
        """The multi-step property. `has_prev` must stay true as it walks,
        and the widen must not re-fire once the queue already holds the
        earlier episodes."""
        pm, _v, media, api = _player(playing="e4")
        seen = []
        for _ in range(3):
            if not media.has_prev:
                pm._widen_queue_backwards(pm._video)
            self.assertTrue(media.has_prev, "ran out of queue at %r" % seen)
            media.seq -= 1
            media.has_prev = media.seq > 0
            seen.append(media.queue[media.seq]["Id"])
        self.assertEqual(seen, ["e3", "e2", "e1"])
        self.assertEqual(api.calls, 1)

    def test_the_first_episode_has_nothing_behind_it(self):
        pm, _v, media, _api = _player(playing="e1")
        self.assertFalse(pm._widen_queue_backwards(pm._video))
        self.assertEqual(len(media.queue), 1)

    def test_syncplay_owns_its_own_queue(self):
        """request_prev is the whole protocol for this; inventing entries
        the group has never heard of is not ours to do."""
        pm, _v, _m, api = _player(playing="e4", syncplay=True)
        self.assertFalse(pm._widen_queue_backwards(pm._video))
        self.assertEqual(api.calls, 0)

    def test_offline_has_nothing_to_ask(self):
        pm, _v, _m, _api = _player(playing="e4", client=False)
        self.assertFalse(pm._widen_queue_backwards(pm._video))

    def test_a_film_is_not_a_series(self):
        pm, _v, _m, api = _player(playing="e4", item_type="Movie")
        self.assertFalse(pm._widen_queue_backwards(pm._video))
        self.assertEqual(api.calls, 0)

    def test_an_episode_missing_from_its_own_series_declines(self):
        # A mixed-in special, or a library that changed underneath us.
        pm, _v, _m, _api = _player(playing="e9")
        self.assertFalse(pm._widen_queue_backwards(pm._video))

    def test_a_server_error_is_not_a_crash_on_a_keypress(self):
        pm, _v, _m, api = _player(playing="e4")
        api.get_episodes = mock.Mock(side_effect=RuntimeError("down"))
        self.assertFalse(pm._widen_queue_backwards(pm._video))


if __name__ == "__main__":
    unittest.main()
