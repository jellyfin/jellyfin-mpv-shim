"""The queue revision: what tells the Play Queue page the queue changed.

The page used to infer a change from the playstate -- a stop, or a playing
id it did not list -- and so never saw a queue changed while the same item
kept playing (remote Play Next/Last, a SyncPlay queue update). The player
now stamps every queue write with a revision, and the page re-reads when
the stamp moves (owner ruling, 2026-10-17: "Player reports changes").

The revision belongs to the QUEUE, not to the Media object: an advance
builds a new Media over the same queue (get_next), and that must not read
as a change, or the page re-reads on every track.
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
import threading
import types
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim import media  # noqa: E402


def _video(item_id, parent, *a, **kw):
    return types.SimpleNamespace(item_id=item_id, parent=parent, is_tv=False)


class MediaRevisionTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(media, "build_video", _video)
        p.start()
        self.addCleanup(p.stop)
        self.m = media.Media(None, ["a", "b", "c"], seq=0)

    def test_a_new_queue_has_its_own_revision(self):
        other = media.Media(None, ["a", "b", "c"], seq=0)
        self.assertNotEqual(self.m.queue_rev, other.queue_rev)

    def test_moving_through_the_queue_keeps_it(self):
        """get_next/get_prev/get_from_key build a new Media over the SAME
        queue. Three steps, and the stamp must not walk."""
        rev = self.m.queue_rev
        m = self.m
        for _ in range(2):
            m = m.get_next()
            self.assertEqual(m.queue_rev, rev)
        m = m.get_prev()
        self.assertEqual(m.queue_rev, rev)
        m = m.get_from_key(m.queue[0]["PlaylistItemId"])
        self.assertEqual(m.queue_rev, rev)

    def test_every_insert_moves_it(self):
        seen = {self.m.queue_rev}
        for append in (False, True, False):
            self.m.insert_items(["x"], append=append)
            self.assertNotIn(self.m.queue_rev, seen)
            seen.add(self.m.queue_rev)

    def test_an_in_place_replace_moves_it(self):
        rev = self.m.queue_rev
        new = [{"Id": "a", "PlaylistItemId": "s1"},
               {"Id": "z", "PlaylistItemId": "s2"}]
        self.assertIsNone(self.m.replace_queue(new, 0))
        self.assertNotEqual(self.m.queue_rev, rev)

    def test_a_replace_that_moves_on_is_a_new_queue(self):
        new = [{"Id": "q", "PlaylistItemId": "s1"}]
        other = self.m.replace_queue(new, 0)
        self.assertIsNotNone(other)
        self.assertNotEqual(other.queue_rev, self.m.queue_rev)

    def test_the_stamp_follows_the_write(self):
        """A reader that sees the new revision must see the new queue."""
        seen = []

        class Spy(media.Media):
            def __setattr__(self, name, value):
                if name in ("queue", "queue_rev"):
                    seen.append(name)
                object.__setattr__(self, name, value)

        m = Spy(None, ["a", "b"], seq=0)
        seen.clear()
        m.insert_items(["x"])
        self.assertEqual(seen, ["queue", "queue_rev"])


def _player(m, item_id):
    from jellyfin_mpv_shim.player import PlayerManager

    pm = PlayerManager.__new__(PlayerManager)
    pm._lock = threading.RLock()
    pm._video = types.SimpleNamespace(parent=m, item_id=item_id)
    return pm


class PlayerReportsTheRevisionTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(media, "build_video", _video)
        p.start()
        self.addCleanup(p.stop)
        self.m = media.Media(None, ["a", "b", "c", "d", "e"], seq=1)
        self.pm = _player(self.m, "b")

    def pids(self):
        return [q["PlaylistItemId"] for q in self.m.queue]

    def test_get_queue_carries_it(self):
        self.assertEqual(self.pm.get_queue()["queue_rev"], self.m.queue_rev)

    def test_player_edits_move_it(self):
        rev = self.m.queue_rev
        self.pm.queue_reorder(list(reversed(self.pids())))
        self.assertNotEqual(self.m.queue_rev, rev)
        rev = self.m.queue_rev
        self.pm.queue_remove_many([self.pids()[-1]])
        self.assertNotEqual(self.m.queue_rev, rev)


class PlayerShuffleTest(unittest.TestCase):
    """Shuffle is decided in the player, under its lock, from what it is
    playing now -- the page's copy of the playing entry went stale on the
    first track change (review finding F1)."""

    def setUp(self):
        p = mock.patch.object(media, "build_video", _video)
        p.start()
        self.addCleanup(p.stop)
        self.m = media.Media(None, ["a", "b", "c", "d", "e"], seq=0)
        self.pm = _player(self.m, "a")

    def ids(self):
        return [q["Id"] for q in self.m.queue]

    def shuffle(self):
        from jellyfin_mpv_shim import player
        with mock.patch.object(player.random, "shuffle",
                               lambda xs: xs.reverse()):
            return self.pm.queue_shuffle_upcoming()

    def test_after_an_advance_the_playing_entry_stays_put(self):
        # The track advanced twice since any page loaded.
        nxt = self.m.get_next().get_next()
        self.pm._video = types.SimpleNamespace(parent=nxt, item_id="c")
        m = nxt
        self.assertTrue(self.shuffle())
        self.assertEqual([q["Id"] for q in m.queue], ["a", "b", "c", "e", "d"])
        self.assertEqual(m.queue[m.seq]["Id"], "c")

    def test_repeated_shuffles_never_move_it(self):
        for _ in range(3):
            self.assertTrue(self.shuffle())
            self.assertEqual(self.m.queue[self.m.seq]["Id"], "a")
            self.assertEqual(self.m.seq, 0)

    def test_too_little_left_to_shuffle(self):
        self.m.seq = 3
        rev = self.m.queue_rev
        self.assertFalse(self.shuffle())
        self.assertEqual(self.m.queue_rev, rev)

    def test_it_moves_the_revision(self):
        rev = self.m.queue_rev
        self.shuffle()
        self.assertNotEqual(self.m.queue_rev, rev)


class SyncPlayReplaceWaitsForTheLockTest(unittest.TestCase):
    """`Media.replace_queue` writes the queue and seq as two stores, and
    get_queue/queue_reorder/queue_remove_many read them as a pair under the
    player lock. SyncPlay's update ran it with no lock at all (review
    finding F8; owner ruling 2026-10-17: "Lock SyncPlay's replace")."""

    def test_it_does_not_run_while_the_player_holds_the_lock(self):
        with mock.patch.object(media, "build_video", _video):
            m = media.Media(None, ["a", "b"], seq=0)
        pm = _player(m, "a")
        new = [{"Id": "a", "PlaylistItemId": "s1"},
               {"Id": "z", "PlaylistItemId": "s2"}]
        held, done = threading.Event(), threading.Event()
        release = threading.Event()

        def holder():
            with pm._lock:
                held.set()
                release.wait(5)
        threading.Thread(target=holder, daemon=True).start()
        self.assertTrue(held.wait(5))
        threading.Thread(
            target=lambda: (pm.replace_queue_locked(m, new, 0), done.set()),
            daemon=True).start()
        self.assertFalse(done.wait(0.2), "replaced while the lock was held")
        self.assertEqual([q["Id"] for q in m.queue], ["a", "b"])
        release.set()
        self.assertTrue(done.wait(5))
        self.assertEqual([q["Id"] for q in m.queue], ["a", "z"])


if __name__ == "__main__":
    unittest.main()
