"""Stopping and rejoining a group, with the real player in the loop.

Every other SyncPlay test in this repo drives a *stand-in* player. That is the
right call for the protocol questions -- the fakes are honest about the
contract (`tests/test_syncplay_player_contract.py` enforces it) and they make
those suites fast and deterministic. But the halt/leave/resume logic is not a
protocol question. It is wiring:

    playerManager.stop()
      -> _release_syncplay()             which branch? asks the UI hook
        -> syncplay.halt_group_playback()  or disable_sync_play()

    syncplay.resume_group_playback()
      -> _start_queue() -> Media -> playerManager.play()

and the liability there is not the message on the wire, it is that the two
objects stop agreeing about who calls what. A fake player cannot fail that
way; it is written to agree.

So this is the one SyncPlay suite with a real `PlayerManager`, a real mpv, a
real stream, and a real group. The *other* member is a stand-in, because the
friend is not the thing under test -- being in a group with someone is.

E2 tier: needs the server, mpv and a display.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402
from _syncplay_live import LiveMember, wait_until  # noqa: E402

SHOW = "The Standard Show"


@_e2e.require_server_and_mpv
class SyncPlayPlaybackTest(_e2e.E2ETestCase):
    """The real player, in a real group, with one stand-in friend."""

    def setUp(self):
        super().setUp()
        # Our own session needs a socket: SyncPlay is entirely server-pushed,
        # and E2ETestCase's session deliberately has none.
        self.session.stop()
        self.session = _e2e.Session(
            self.account, device_id=_e2e.DEVICE_PREFIX + "sp-player",
            websocket=True)
        self.addCleanup(self.session.stop)

        self.pm = type(self).pm
        sp = self.pm.syncplay
        sp.client = self.session.client
        self.session.listeners.append(self._route)

        # The branch under test is chosen by this hook, which in the app is
        # the browser answering "is the SyncPlay menu reachable after this
        # stop?". Default it to yes; the leaving test flips it.
        self.pm.syncplay_menu_reachable = lambda: True
        self.addCleanup(setattr, self.pm, "syncplay_menu_reachable", None)

        self.friend = LiveMember("friend", "qa-admin",
                                 _e2e.DEVICE_PREFIX + "sp-friend")
        self.addCleanup(self.friend.stop)
        self.addCleanup(self._leave_group)

        # Registered last so it runs FIRST (cleanups are LIFO): the player's
        # final "stopped" report needs a token that is still valid, and the
        # session cleanups above would have revoked it.
        self.addCleanup(self._safe_stop)

        episodes = self.session.episodes(SHOW, season=1)
        self.assertGreaterEqual(len(episodes), 2,
                                "need two episodes to tell one from the other")
        self.item_id = episodes[0]["Id"]
        self.other_id = episodes[1]["Id"]

    def _route(self, event_name, data):
        """What `event_handler` does, against the real playerManager."""
        sp = self.pm.syncplay
        if event_name == "SyncPlayCommand":
            sp.client = self.session.client
            sp.process_command(dict(data))
        elif event_name == "SyncPlayGroupUpdate":
            sp.client = self.session.client
            sp.process_group_update(dict(data))

    def _watch_player(self, *names):
        """Record calls the group makes into the player, and return the log.

        Instance attributes shadowing the bound methods, removed on cleanup so
        the process-wide player is handed back unmodified -- it outlives this
        class (see `ensure_real_player`).
        """
        calls = []

        def spy(name, real):
            def recorded(*args, **kwargs):
                calls.append((name, args))
                return real(*args, **kwargs)
            return recorded

        for name in names:
            setattr(self.pm, name, spy(name, getattr(self.pm, name)))
            self.addCleanup(delattr, self.pm, name)
        return calls

    def _leave_group(self):
        try:
            if self.pm.syncplay.in_group():
                self.session.client.jellyfin.leave_sync_play()
        except Exception:
            pass
        wait_until(lambda: not self.pm.syncplay.in_group(), timeout=10)

    # -- getting into a group and playing --------------------------------

    def join_group(self):
        """Get both of us into a group, with nothing playing yet.

        Separate from starting something because "in a group, nothing on" is
        a state the app can now sit in -- the browser's SyncPlay dialog makes
        a group from the home screen -- and it is where the interesting
        failures live.
        """
        self.session.client.jellyfin.new_sync_play_v2("jms-e2e player")
        self.assertTrue(wait_until(lambda: self.pm.syncplay.in_group(),
                                   timeout=15),
                        "our player never joined the group it created")
        group_id = self.pm.syncplay.current_group
        self.friend.api.join_sync_play(group_id)
        self.assertTrue(wait_until(lambda: self.friend.manager.in_group(),
                                   timeout=15), "the friend never joined")
        return group_id

    def join_and_play(self, items=None):
        """A group, with the friend putting something on.

        Returns once our real mpv is genuinely decoding: the position moving
        is the only proof that the wiring from a PlayQueue update through
        `Media` to `playerManager.play` actually ran.
        """
        group_id = self.join_group()
        self.friend.api.reset_queue_sync_play(list(items or [self.item_id]),
                                              0, 0)
        self.assertTrue(
            self.pump_until(lambda: self.pm.get_video() is not None, 45),
            "the group's queue never reached the real player")
        self.assertTrue(
            self.pump_until(
                lambda: (self.pm._player.playback_time or 0) > 0.3, 45),
            "mpv never started decoding the group's episode")
        return group_id

    # -- pressing play while already in a group --------------------------

    def press_play(self, item_ids=None):
        """What the browser does when the user picks something."""
        from jellyfin_mpv_shim import event_handler

        return event_handler.start_playback(
            self.session.client, list(item_ids or [self.item_id]))

    def test_pressing_play_while_in_a_group_actually_plays(self):
        """The reported bug, end to end.

        Make a group from the home screen with nothing playing, then pick
        something. This raised 400 from SyncPlay/Ready on the way in: the
        locally built Media had invented its playlist ids and the server
        declares that field a Guid, so the load unwound as a failed start and
        the user got "playback could not be started" for a perfectly good
        file.
        """
        self.join_group()
        self.assertTrue(self.pm.syncplay.is_enabled())

        self.press_play()

        self.assertTrue(
            self.pump_until(lambda: self.pm.get_video() is not None, 45),
            "pressing play in a group started nothing")
        self.assertTrue(
            self.pump_until(
                lambda: (self.pm._player.playback_time or 0) > 0.3, 45),
            "pressing play in a group loaded something that never decoded")
        self.assertEqual(self.pm.get_video().item_id, self.item_id)

    def test_pressing_play_puts_it_on_for_the_group(self):
        """And it reaches the other members, which is the point of being in a
        group at all. Playing it only locally would leave them on whatever
        they had -- the failure the 400 was hiding."""
        self.join_group()

        self.press_play()

        self.assertTrue(
            wait_until(lambda: self.friend.player.video is not None,
                       timeout=25),
            "we pressed play in a group and the other member was never told")

    def test_backing_out_and_choosing_something_else_moves_the_group(self):
        """Stop, then pick something different: the group follows you.

        This is the intuition-defying half, and it is web's behaviour, read
        off the shape of its code rather than its docs --
        `isFollowingGroupPlayback` is consulted in exactly three places, all
        inside QueueCore, and none of them is the play path. So backing out of
        what the group is watching and choosing something else is *how you
        change what the group watches*, and the NewPlaylist that comes back
        re-attaches you. Letting a halted member play privately instead reads
        as considerate and leaves them unable to put anything on at all.
        """
        self.join_and_play()
        self.pm.stop()
        self.assertTrue(self.pm.syncplay.is_halted())

        self.press_play([self.other_id])

        self.assertTrue(
            wait_until(lambda: self.friend.player.video is not None
                       and self.friend.player.video.item_id == self.other_id,
                       timeout=25),
            "we picked something new after stopping and the group was never "
            "moved to it")
        self.assertTrue(
            self.pump_until(lambda: self.pm.get_video() is not None
                            and self.pm.get_video().item_id == self.other_id,
                            45),
            "we picked something new and never started playing it ourselves")
        self.assertTrue(
            self.pm.syncplay.is_enabled(),
            "the new queue did not re-attach us; we would be watching what "
            "we chose while the group thinks we have stopped")

    def test_closing_the_window_leaves_the_group(self):
        """Whatever else a stop does, a closing window leaves.

        Every other stop halts, because you can come back to the library and
        the SyncPlay menu. A closing window has no library behind it -- the
        app is quitting or going to the tray -- so a halted membership there
        is one nobody can see, leave or resume while the group waits on it.
        """
        group_id = self.join_and_play()

        self.pm.stop_for_window_close()

        self.assertFalse(self.pm.syncplay.in_group(),
                         "closing the window left us in the group")
        self.assertTrue(wait_until(
            lambda: all(len(g.get("Participants") or []) <= 1
                        for g in (self.friend.api.get_sync_play() or [])
                        if g.get("GroupId") == group_id),
            timeout=15), "the server still lists us in the group")

    def test_backing_out_keeps_up_with_the_groups_queue(self):
        """Backed out, the group moves through its queue, then you resume.

        A NextItem specifically, not a new playlist: a new playlist pulls a
        halted member back in and starts playing, so it cannot show what this
        is about. The group moving *within* its queue must not restart a
        player the user stopped -- and must still be recorded, or Resume
        rejoins whatever was on when they stopped rather than what everyone
        is watching now.
        """
        self.join_and_play([self.item_id, self.other_id])
        self.pm.stop()
        self.assertTrue(self.pm.syncplay.is_halted())

        self.friend.manager.request_next(
            self.friend.player.video.get_playlist_id())

        self.assertTrue(
            wait_until(lambda: self.friend.player.video is not None
                       and self.friend.player.video.item_id == self.other_id,
                       timeout=20),
            "the friend never advanced, so there is nothing to have missed")
        # Pumped for a while rather than read once: a real load takes a
        # moment, so an immediate assertion passes because playback has not
        # started *yet*, which is not the same as never. Verified by making
        # the client follow the update and watching this fail.
        self.assertFalse(
            self.pump_until(lambda: self.pm.get_video() is not None, 8),
            "the group moving through its queue restarted a player the user "
            "had stopped")
        self.assertEqual(
            (self.pm.syncplay.last_playqueue or {}).get("PlayingItemIndex"), 1,
            "we did not record where the group got to, so Resume would "
            "rejoin the item that was playing when we stopped")

    def test_the_group_drives_the_real_player(self):
        """The premise everything below rests on: a queue update on the wire
        ends up as real playback, through the real Media/play path."""
        self.join_and_play()
        video = self.pm.get_video()
        self.assertIsNotNone(video)
        self.assertEqual(video.item_id, self.item_id)

    # -- the branch this branch added ------------------------------------

    def test_stopping_halts_the_group_instead_of_leaving_it(self):
        group_id = self.join_and_play()

        self.pm.stop()

        self.assertTrue(
            self.pm.syncplay.in_group(),
            "stopping playback left the SyncPlay group; the other members "
            "lose you from the group entirely rather than just from what "
            "they are watching")
        self.assertTrue(self.pm.syncplay.is_halted())
        self.assertEqual(self.pm.syncplay.current_group, group_id)
        self.assertIsNone(self.pm.get_video(), "playback did not actually stop")

        # And the server agrees we are still a member.
        groups = self.session.client.jellyfin.get_sync_play() or []
        mine = [g for g in groups if g.get("GroupId") == group_id]
        self.assertTrue(mine, "the server no longer has the group")
        self.assertEqual(
            len(mine[0].get("Participants") or []), 2,
            "the server dropped us from the group: %r" % (mine[0],))

    def test_stopping_leaves_when_the_menu_is_unreachable(self):
        """The other half of `_release_syncplay`, and the reason the hook
        exists: with no way back to a SyncPlay menu -- no GUI, or a cast whose
        library was never opened -- halting would strand the user in a group
        they cannot get out of, so that surface leaves instead."""
        self.join_and_play()
        self.pm.syncplay_menu_reachable = lambda: False

        self.pm.stop()

        self.assertFalse(
            self.pm.syncplay.in_group(),
            "with no SyncPlay menu to leave from later, stopping has to "
            "leave the group -- otherwise the membership is permanent")
        self.assertTrue(wait_until(
            lambda: len(self.friend.api.get_sync_play() or []) == 0
            or all(len(g.get("Participants") or []) == 1
                   for g in self.friend.api.get_sync_play() or []),
            timeout=10), "the server still lists us in the group")

    def test_a_halted_player_is_not_driven_by_the_group(self):
        """After a stop, the group's commands must not reach the player.

        Watched at the player boundary rather than by its effects, because
        with nothing loaded the effects are nearly invisible: a seek on an
        idle mpv changes little and a Ready is suppressed by having no video,
        so "playback did not restart" passes even when every command is being
        applied. What is actually wrong in that state is that the group is
        driving a player the user has walked away from, so that is what this
        watches -- the calls themselves.
        """
        self.join_and_play()
        self.pm.stop()
        self.assertTrue(self.pm.syncplay.is_halted())

        driven = self._watch_player("seek", "set_paused", "set_speed", "play")

        self.friend.manager.seek_request(4.0)
        time.sleep(2.0)
        self.pm.update()

        self.assertEqual(
            driven, [],
            "the group drove a halted player: %r. Nothing the group says may "
            "reach a player the user has stopped -- that is what halting is."
            % (driven,))
        self.assertIsNone(
            self.pm.get_video(),
            "a group seek restarted playback on a player that had stopped")

    def test_resuming_plays_the_groups_content_again(self):
        """The whole point of halting rather than leaving, end to end:
        stop, then come back to what everyone else is still watching --
        through the real Media build and the real player."""
        self.join_and_play()
        self.pm.stop()
        self.assertTrue(self.pm.syncplay.is_halted())

        self.pm.syncplay.resume_group_playback()

        self.assertTrue(self.pm.syncplay.is_enabled(),
                        "resume did not re-attach us to the group")
        self.assertTrue(
            self.pump_until(lambda: self.pm.get_video() is not None, 45),
            "resume re-attached but never started playing anything")
        self.assertTrue(
            self.pump_until(
                lambda: (self.pm._player.playback_time or 0) > 0.3, 45),
            "resume loaded a video that never decoded a frame")
        self.assertEqual(self.pm.get_video().item_id, self.item_id)


if __name__ == "__main__":
    unittest.main()
