"""Two people watching one thing, with a real Jellyfin deciding.

The same property `tests/test_syncplay_e2e.py` asserts against a modelled
server -- everybody at the same place, in the same state, nobody holding the
group up -- but here nothing about the server is a belief of ours. That suite
is faster and can force states this one cannot; this one is the check that the
model is telling the truth. When they disagree, the model is wrong.

Read `tests/e2e/_syncplay_live.py` for what is real (both websockets, the
group, every command, the Media round trip) and what is not (mpv).
"""

import time
import unittest

from tests.e2e import _e2e
from tests.e2e._syncplay_live import (
    TOLERANCE,
    LiveGroup,
    settled,
    wait_until,
)


class GroupCase(unittest.TestCase):
    """One pair of sessions for the class, a fresh group per test.

    Logging in and opening a websocket is the slow part and is independent of
    what any test does; group membership is not, and a group left over from a
    failed test would be joined by the next one.

    No test methods of its own: unittest collects every TestCase subclass in a
    module, so a base that carried tests would run all of them again for each
    concrete class.
    """

    ACCOUNTS = (("alice", "qa-user"), ("bob", "qa-admin"))
    #: Distinct per class so two classes never register the same device id.
    DEVICE_TAG = "a"

    @classmethod
    def setUpClass(cls):
        cls.group = LiveGroup.build(*cls.ACCOUNTS, tag=cls.DEVICE_TAG)
        cls.alice = cls.group["alice"]
        cls.bob = cls.group["bob"]
        cls.addClassCleanup(cls.group.close)
        # Something with a runtime, so a seek to 5s means something. Any
        # episode will do -- SyncPlay carries positions, it does not decode.
        admin = _e2e.Session("qa-admin", device_id=_e2e.DEVICE_PREFIX + "sp-find")
        try:
            episodes = admin.find_all(library="Shows", item_type="Episode")
            cls.item_id = episodes[0]["Id"]
        finally:
            admin.stop()

    def setUp(self):
        # Before create(), not after: a member still holding the last test's
        # video and pause state makes every wait in this file pass on stale
        # evidence.
        self.group.reset()
        self.group.create("alice")
        self.addCleanup(self._leave_all)

    def _leave_all(self):
        for member in self.group.members.values():
            member.leave()
        # The next test's create() must not find us still seated.
        wait_until(lambda: not any(m.manager.in_group()
                                   for m in self.group.members.values()),
                   timeout=10)

    # -- the assertion this file exists for ------------------------------

    def wait_all(self, predicate, timeout=20):
        """Wait until `predicate` holds for EVERY member.

        Waiting on one and asserting about all is the standing trap in this
        file: the member you watched is by definition the one that has already
        arrived, so the assertion runs exactly when the other one is most
        likely to still be in flight.
        """
        return wait_until(
            lambda: all(predicate(m) for m in self.group.members.values()),
            timeout=timeout)

    def assertTogether(self, at=None, playing=None, why=""):
        members = list(self.group.members.values())
        if playing is not None:
            self.assertTrue(
                settled(members),
                "%sthe members never agreed on whether they were playing: %r"
                % (why and why + ": ", members))
            for m in members:
                self.assertEqual(
                    m.playing, playing,
                    "%s%s is %s, expected %s. Everyone: %r"
                    % (why and why + ": ", m.label,
                       "playing" if m.playing else "paused",
                       "playing" if playing else "paused", members))

        # Sampled together rather than one at a time: while playing these are
        # moving, and two reads a round trip apart differ for that reason
        # alone.
        readings = [(m.label, m.position) for m in members]
        spread = max(p for _, p in readings) - min(p for _, p in readings)
        self.assertLessEqual(
            spread, TOLERANCE,
            "%sthe members are %.3fs apart -- they are not watching the same "
            "frame: %r" % (why and why + ": ", spread, readings))

        if at is not None:
            for label, pos in readings:
                self.assertAlmostEqual(
                    pos, at, delta=TOLERANCE,
                    msg="%s%s is at %.3fs, expected %.3fs. Agreeing with each "
                        "other on the wrong frame is not synchronisation: %r"
                        % (why and why + ": ", label, pos, at, readings))

    def assertSeekedTo(self, target, since, why=""):
        """Everyone went to `target`, and has been playing since `since`.

        A plain `at=target` cannot say this: the group resumes after a seek,
        so the position it is being compared against is moving, and whether
        the assertion passes depends on how long the round trip took. The
        window is what the claim actually is -- not before the seek target,
        not further past it than playback could have carried them.
        """
        members = list(self.group.members.values())
        elapsed = time.monotonic() - since
        readings = [(m.label, m.position) for m in members]
        spread = max(p for _, p in readings) - min(p for _, p in readings)
        self.assertLessEqual(
            spread, TOLERANCE,
            "%sthe members are %.3fs apart after a seek: %r"
            % (why and why + ": ", spread, readings))
        for label, pos in readings:
            self.assertGreaterEqual(
                pos, target - TOLERANCE,
                "%s%s is at %.3fs, behind the %.3fs the group seeked to: %r"
                % (why and why + ": ", label, pos, target, readings))
            self.assertLessEqual(
                pos, target + elapsed + TOLERANCE,
                "%s%s is at %.3fs, further past %.3fs than %.1fs of playback "
                "could carry it: %r"
                % (why and why + ": ", label, pos, target, elapsed, readings))


@_e2e.require_server
class SyncPlayGroupTest(GroupCase):
    """Does a group of two stay together at all?"""

    DEVICE_TAG = "basic"

    # -- membership -------------------------------------------------------

    def test_both_clients_are_in_the_group_the_server_knows_about(self):
        self.assertTrue(self.alice.manager.in_group())
        self.assertTrue(self.bob.manager.in_group())
        self.assertEqual(self.alice.manager.current_group,
                         self.bob.manager.current_group)

        participants = self.group.participants()
        self.assertEqual(len(participants), 2,
                         "the server lists %r in the group" % (participants,))

    def test_leaving_takes_you_out_of_the_group(self):
        self.bob.leave()
        self.assertTrue(
            wait_until(lambda: not self.bob.manager.in_group()),
            "bob left the group and the server never told him so")
        self.assertTrue(self.alice.manager.in_group(),
                        "bob leaving took alice out too")

    # -- playback ---------------------------------------------------------

    def test_starting_the_queue_starts_everybody(self):
        self.group.start([self.item_id])
        self.assertTrue(
            self.wait_all(lambda m: m.playing),
            "the group never started playing: %r" % (self.group.members,))
        self.assertTogether(playing=True, why="after the queue was set")

    def test_a_pause_reaches_the_other_member(self):
        self.group.start([self.item_id])
        self.wait_all(lambda m: m.playing)

        self.alice.manager.pause_request()
        self.assertTrue(
            wait_until(lambda: self.bob.paused, timeout=15),
            "alice paused and bob played on")
        self.assertTogether(playing=False, why="after alice paused")

    def test_either_member_can_drive(self):
        """Control is not the property of whoever made the group."""
        self.group.start([self.item_id])
        self.wait_all(lambda m: m.playing)

        self.bob.manager.pause_request()
        self.assertTrue(wait_until(lambda: self.alice.paused, timeout=15),
                        "bob paused and alice played on")

        self.alice.manager.play_request()
        self.assertTrue(wait_until(lambda: self.bob.playing, timeout=15),
                        "alice resumed and bob stayed paused")
        self.assertTogether(playing=True, why="after alice resumed")

    def test_a_seek_takes_everyone_with_it(self):
        self.group.start([self.item_id])
        self.wait_all(lambda m: m.playing)

        since = time.monotonic()
        self.alice.manager.seek_request(5.0)
        self.assertTrue(
            self.wait_all(lambda m: m.position >= 5.0 - TOLERANCE),
            "not everybody followed the seek to 5s: %r"
            % (self.group.members,))
        self.assertSeekedTo(5.0, since, why="after a seek to 5s")

    def test_the_group_resumes_after_a_seek(self):
        """A seek puts the group into Waiting and it comes out only when every
        member reports Ready. A client that never answers leaves everybody
        stopped -- the classic SyncPlay hang, which from the inside looks like
        "playback just ended" and reports nothing anywhere.

        Separate from the seek test above because the positions are right
        either way: everyone lands on the target and then sits there.
        """
        self.group.start([self.item_id])
        self.wait_all(lambda m: m.playing)

        self.bob.manager.seek_request(5.0)
        self.assertTrue(
            self.wait_all(lambda m: m.playing),
            "the group never resumed after a seek -- alice %s, bob %s"
            % (self.alice.player, self.bob.player))

    def test_they_stay_together_while_playing(self):
        """Not a snapshot: sampled across several seconds of real playback,
        because drift is a thing that accumulates and a single reading taken
        right after a command cannot see it."""
        self.group.start([self.item_id])
        self.assertTrue(self.wait_all(lambda m: m.playing),
                        "the group never started")

        worst = 0.0
        for _ in range(12):
            worst = max(worst, abs(self.alice.position - self.bob.position))
            time.sleep(0.25)
        self.assertLessEqual(
            worst, TOLERANCE,
            "the two clients drifted %.3fs apart over three seconds of "
            "playback" % worst)


@_e2e.require_server
class HaltingIsNotLeavingTest(GroupCase):
    """Stopping playback halts the group rather than leaving it, and this is
    that claim checked against the server that has to honour it.

    The offline suite asserts the same things against a ported
    `SetIgnoreWait`; the whole reason to run it again here is that the port
    could be wrong about the endpoint that makes it work.
    """

    DEVICE_TAG = "halt"

    def test_halting_keeps_the_membership(self):
        self.group.start([self.item_id])
        self.wait_all(lambda m: m.playing)

        self.alice.manager.halt_group_playback()

        self.assertTrue(self.alice.manager.is_halted())
        self.assertTrue(self.alice.manager.in_group(),
                        "halting gave up the membership")
        self.assertEqual(len(self.group.participants()), 2,
                         "the server dropped the halted member from the group")

    def test_a_halted_member_does_not_stall_the_group(self):
        """The point of SetIgnoreWait, and the one thing here that a modelled
        server genuinely cannot vouch for: a seek puts the group into Waiting
        and it resumes only when everyone it is waiting for reports Ready. A
        halted member never will."""
        self.group.start([self.item_id])
        self.wait_all(lambda m: m.playing)

        self.alice.manager.halt_group_playback()
        self.bob.manager.seek_request(5.0)

        self.assertTrue(
            wait_until(lambda: self.bob.playing
                       and abs(self.bob.position - 5.0) < 3.0, timeout=20),
            "bob is stuck at %.3fs %s after seeking -- the server is still "
            "waiting on a member who has stopped watching, which is exactly "
            "what SetIgnoreWait is for"
            % (self.bob.position, "playing" if self.bob.playing else "paused"))

    def test_the_halted_member_is_not_driven_by_the_group(self):
        self.group.start([self.item_id])
        self.wait_all(lambda m: m.playing)

        self.alice.manager.halt_group_playback()
        self.alice.player.seeks.clear()
        self.bob.manager.seek_request(5.0)
        wait_until(lambda: abs(self.bob.position - 5.0) < 3.0, timeout=20)
        # Give anything mis-addressed to alice time to arrive.
        time.sleep(1.0)

        self.assertEqual(self.alice.player.seeks, [],
                         "the group seeked a member who had stopped watching")

    def test_resuming_rejoins_the_groups_playback(self):
        self.group.start([self.item_id])
        self.wait_all(lambda m: m.playing)

        self.alice.manager.halt_group_playback()
        self.bob.manager.seek_request(30.0)
        wait_until(lambda: abs(self.bob.position - 30.0) < 3.0, timeout=20)

        self.alice.manager.resume_group_playback()

        self.assertTrue(self.alice.manager.is_enabled(),
                        "resume did not re-attach alice to the group")
        self.assertTrue(
            wait_until(lambda: abs(self.alice.position - self.bob.position)
                       <= TOLERANCE, timeout=20),
            "alice resumed at %.3fs while the group is at %.3fs"
            % (self.alice.position, self.bob.position))


if __name__ == "__main__":
    unittest.main()
