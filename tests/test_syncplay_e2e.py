"""Two people watching one thing: does SyncPlay actually keep them together?

Every other SyncPlay test in this suite asks a question about one client --
did it answer Ready, did it tear its timers down, did it record the command.
Those are necessary and they are not the feature. The feature is a property
of a *group*, and no test has ever been able to state it, because there has
only ever been one real client on the wire: everyone else was a session
record that never reacted to anything.

``tests/_syncplay_network`` seats several real ``SyncPlayManager``s on one
group with a message bus between them, so these can assert the thing people
actually complain about when SyncPlay is broken:

    after anything anyone does, everybody is at the same place, in the same
    state, and the group is not stuck waiting for someone.

That is ``assertTogether``, and it is the point of the file. It is written
as one assertion so that a regression names the divergence -- who is where --
rather than reporting that some call was not made.

Two things worth knowing when reading a failure here:

* **Convergence is asserted against the group, not just between members.**
  Two clients that agree with each other and are both a minute behind the
  server have not synced; they have merely failed identically, and an
  each-other-only check is exactly the check that would pass.
* **The step counter is an assertion.** Settling costs a handful of
  messages; the bus fails the test if an action does not settle, so a
  livelock shows up here rather than as a hung suite.
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

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from tests._syncplay_network import SyncPlayNetwork  # noqa: E402
from tests._syncplay_server import (  # noqa: E402
    PAUSED,
    PLAYING,
    WAITING,
)

START = 300.0          # five minutes in, so "went back to zero" is visible


class GroupCase(unittest.TestCase):
    #: The clients compute their positions from wall-clock arithmetic against
    #: the group's, so exact equality is not on offer. A tenth of a second is
    #: far tighter than the 500 ms the server itself calls "lost in time", and
    #: far looser than the microseconds these actually land within.
    TOLERANCE = 0.1

    def network(self, members=("alice", "bob"), state=PLAYING, position=START):
        net = SyncPlayNetwork(state=state,
                              position_ticks=int(position * 10_000_000))
        for name in members:
            net.seat(name)
        return net

    def assertTogether(self, net, playing=None, at=None, why=""):
        """Everyone is at the same position, in the same state, and nobody is
        holding the group up. The whole feature, in one assertion."""
        members = list(net.members.values())
        self.assertTrue(members, "no members to compare")

        where = at if at is not None else net.group_position
        for m in members:
            self.assertAlmostEqual(
                m.position, where, delta=self.TOLERANCE,
                msg="%s%s is at %.3fs but the group is at %.3fs -- the "
                    "members are not watching the same frame. Everyone: %r"
                    % (why and why + ": ", m.name, m.position, where, members))

        if playing is not None:
            for m in members:
                self.assertEqual(
                    m.playing, playing,
                    "%s%s is %s while the group is %s. Everyone: %r"
                    % (why and why + ": ", m.name,
                       "playing" if m.playing else "paused",
                       "playing" if playing else "paused", members))

        self.assertEqual(
            net.group.waiting_on(), [],
            "%sthe group never left Waiting -- it is still holding for %r, "
            "which stops playback for everybody, silently, on both ends"
            % (why and why + ": ", net.group.waiting_on()))


class TestTheyStartTogether(GroupCase):
    def test_two_clients_seated_on_a_playing_group_agree(self):
        net = self.network()
        self.assertTogether(net, playing=True, at=START)


class TestOnePersonPauses(GroupCase):
    """The thing SyncPlay is for. One person hits pause; everyone stops, at
    the same frame, and the group knows it is paused."""

    def test_a_pause_reaches_the_other_member(self):
        net = self.network()
        net.members["alice"].manager.pause_request()

        self.assertEqual(net.group.state, PAUSED)
        self.assertTogether(net, playing=False, why="after alice paused")

    def test_the_other_member_can_unpause_it_again(self):
        """Both directions: control is not the property of whoever started."""
        net = self.network()
        net.members["alice"].manager.pause_request()
        net.members["bob"].manager.play_request()

        self.assertEqual(net.group.state, PLAYING)
        self.assertTogether(net, playing=True, why="after bob resumed")

    def test_a_pause_does_not_rewind_anybody(self):
        """A pause is not a seek. The group credits itself the time it really
        played and everyone stays where they were, give or take the moment
        the message spent in flight."""
        net = self.network()
        net.members["bob"].manager.pause_request()
        self.assertTogether(net, playing=False, at=START,
                            why="a pause moved the playhead")


class TestOnePersonSeeks(GroupCase):
    """A Seek is the one command that puts the whole group into Waiting, so
    it is the one that strands everybody when a client does not answer."""

    def test_a_seek_takes_everyone_with_it(self):
        net = self.network()
        net.members["alice"].manager.seek_request(900.0)

        self.assertTogether(net, playing=True, at=900.0,
                            why="after alice seeked to 15:00")

    def test_a_seek_backwards_works_too(self):
        net = self.network()
        net.members["bob"].manager.seek_request(30.0)
        self.assertTogether(net, playing=True, at=30.0)

    def test_the_group_does_not_stay_in_waiting(self):
        """Explicitly, and not only via assertTogether: a group that never
        leaves Waiting is the classic SyncPlay hang, and it looks like
        "playback just stopped" to every member including the one who seeked.
        """
        net = self.network()
        net.members["alice"].manager.seek_request(900.0)

        self.assertEqual(net.group.state, PLAYING,
                         "the group is still %s after a seek everybody "
                         "answered" % net.group.state)

    def test_a_seek_settles_without_a_storm(self):
        """Convergence has to be cheap. Each member owes one Ready; if a
        seek costs a message per member per round the group is oscillating,
        which on a real server is a visible stutter for everyone."""
        net = self.network(members=("alice", "bob", "carol"))
        before = net.steps
        net.members["alice"].manager.seek_request(900.0)
        self.assertLess(net.steps - before, 20,
                        "a single seek cost %d messages"
                        % (net.steps - before))


class TestOnePersonStalls(GroupCase):
    """The group pauses for a member whose stream stalled, and resumes when
    they catch up. This is the behaviour the mock server did not model until
    now, so nothing had ever exercised the client's side of it."""

    def stall(self, net, name):
        member = net.members[name]
        member.manager._buffer_req(True)
        member.manager.is_buffering = True
        return member

    def test_everyone_else_pauses_for_a_stalled_member(self):
        net = self.network(members=("alice", "bob", "carol"))
        self.stall(net, "alice")

        self.assertEqual(net.group.state, WAITING)
        self.assertEqual(net.group.waiting_on(), ["alice"])
        self.assertFalse(net.members["bob"].playing,
                         "bob played on while alice was buffering -- he ends "
                         "up ahead and the group never re-syncs him")
        self.assertFalse(net.members["carol"].playing)

    def test_everyone_resumes_when_they_recover(self):
        net = self.network(members=("alice", "bob", "carol"))
        self.stall(net, "alice")
        net.members["alice"].manager.on_buffer_done()

        self.assertEqual(net.group.state, PLAYING)
        self.assertTogether(net, playing=True, why="after alice recovered")

    def test_a_stall_does_not_lose_the_position(self):
        net = self.network()
        self.stall(net, "bob")
        net.members["bob"].manager.on_buffer_done()
        self.assertTogether(net, playing=True, at=START,
                            why="a stall and recovery moved the playhead")


class TestSomebodyArrivesLate(GroupCase):
    """Joining a group that is already watching something. The server puts
    the group into Waiting for the newcomer, which means an arrival that goes
    wrong stops the film for the people who were already there."""

    def test_the_newcomer_is_brought_to_the_groups_position(self):
        net = self.network()
        net.members["alice"].manager.seek_request(900.0)
        net.join("carol")

        self.assertIn("carol", net.members)
        self.assertTogether(net, at=900.0,
                            why="after carol joined a group at 15:00")

    def test_the_people_already_watching_are_not_left_paused(self):
        net = self.network()
        net.join("carol")
        self.assertTogether(net, playing=True,
                            why="carol's arrival stopped the film")


class TestAMemberWhoHasDrifted(GroupCase):
    """"One person is always a few seconds behind" is the other thing people
    report about SyncPlay, and the server has two mechanisms for it. Both are
    easy to break in ways that are invisible from the drifting client's own
    logs, because in both the *server* speaks and the client is supposed to
    listen."""

    def test_a_member_reporting_the_wrong_position_is_pulled_back(self):
        """``WaitingGroupState``'s "Session got lost in time, correcting":
        a Ready from more than 500 ms out earns a corrective Seek, and the
        member stays buffering until it answers from the right place."""
        net = self.network()
        alice, bob = net.members["alice"], net.members["bob"]
        bob.manager.seek_request(900.0)

        alice.player.position = 12.0       # fifteen minutes behind
        alice.player.paused = True
        alice.manager._buffer_req(True)
        alice.manager.is_buffering = True
        alice.manager.on_buffer_done()

        self.assertTogether(net, at=900.0,
                            why="a member who reported the wrong position was "
                                "left there")

    def test_the_servers_resync_is_not_mistaken_for_a_duplicate(self):
        """A client whose player has fallen out of step asks the server where
        the group is. The answer is byte-identical to the last broadcast
        except for ``EmittedAt`` -- deliberately, it is a *resend of the
        current state* -- so a duplicate filter that ignores that field
        discards the only correction channel there is, and the member stays
        stuck with no error anywhere.
        """
        net = self.network()
        alice = net.members["alice"]

        alice.manager.play_request()
        alice.player.paused = True         # drifts out of step again
        alice.manager.play_request()       # "client got lost, sending state"

        self.assertTrue(
            alice.playing,
            "alice asked the server to resync her and it did, but the "
            "resend was discarded as a duplicate -- she stays paused while "
            "everyone else watches on")
        self.assertTogether(net, playing=True, why="after a resync")


class TestStoppingIsNotLeaving(GroupCase):
    """The change this branch made, seen from the other members' side.

    Alice stopping playback must be invisible to Bob: he keeps watching, the
    group keeps running, and it never waits on her again.
    """

    def test_one_member_halting_does_not_disturb_the_others(self):
        net = self.network()
        alice, bob = net.members["alice"], net.members["bob"]
        alice.manager.halt_group_playback()

        self.assertTrue(alice.manager.is_halted())
        self.assertTrue(bob.manager.is_enabled(),
                        "alice stopping took bob out of the group")
        self.assertTrue(bob.playing, "alice stopping paused bob")
        self.assertEqual(net.group.state, PLAYING)

    def test_a_halted_member_never_holds_the_group_again(self):
        """The point of SetIgnoreWait. Without it the group enters Waiting on
        the next seek and sits there forever, because the member it is
        waiting for has stopped watching and will never report Ready."""
        net = self.network()
        net.members["alice"].manager.halt_group_playback()
        net.members["bob"].manager.seek_request(900.0)

        self.assertEqual(net.group.waiting_on(), [],
                         "the group is held up by a member who has stopped")
        self.assertEqual(net.group.state, PLAYING)
        self.assertAlmostEqual(net.members["bob"].position, 900.0,
                               delta=self.TOLERANCE)

    def test_the_halted_member_is_not_driven_by_the_group(self):
        net = self.network()
        alice = net.members["alice"]
        alice.manager.halt_group_playback()
        alice.player.seeks.clear()
        alice.player.paused = None          # a value the group never sets

        net.members["bob"].manager.seek_request(900.0)
        net.members["bob"].manager.pause_request()

        self.assertEqual(alice.player.seeks, [],
                         "a halted member was seeked by the group")
        self.assertIsNone(alice.player.paused,
                          "a halted member was paused by the group")

    def test_resuming_brings_them_back_to_where_everyone_is(self):
        net = self.network()
        alice, bob = net.members["alice"], net.members["bob"]
        alice.manager.halt_group_playback()
        bob.manager.seek_request(900.0)

        # Alice comes back. The queue she stored is the one the group is on,
        # and the commands she recorded while halted say where it has got to.
        started = []
        alice.manager._start_queue = lambda data, offset, **kw: started.append(offset)
        alice.manager.last_playqueue = {
            "Playlist": [{"PlaylistItemId": "pl-1", "ItemId": "item-1"}],
            "PlayingItemIndex": 0, "StartPositionTicks": 0,
        }
        alice.manager.resume_group_playback()

        self.assertTrue(alice.manager.is_enabled(), "resume did not re-attach")
        self.assertEqual(len(started), 1, "resume played nothing")
        self.assertAlmostEqual(
            started[0], 900.0, delta=1.0,
            msg="alice resumed at %.3fs while the group is at 900s -- the "
                "stored queue's start position was used instead of the "
                "commands recorded while she was away" % started[0])


class TestTheGroupSurvivesASequence(GroupCase):
    """One long session rather than one operation, because the failures this
    file exists to catch are the ones that need a particular history: a
    position that drifts a little on every round trip, or a member that stops
    answering after some earlier state."""

    def test_a_realistic_evening_stays_in_sync(self):
        net = self.network(members=("alice", "bob", "carol"))
        alice, bob, carol = (net.members[n] for n in ("alice", "bob", "carol"))

        alice.manager.pause_request()
        self.assertTogether(net, playing=False, why="after the first pause")

        bob.manager.play_request()
        self.assertTogether(net, playing=True, why="after bob resumed")

        carol.manager.seek_request(1200.0)
        self.assertTogether(net, playing=True, at=1200.0, why="after a seek")

        carol.manager._buffer_req(True)
        carol.manager.is_buffering = True
        carol.manager.on_buffer_done()
        self.assertTogether(net, playing=True, at=1200.0,
                            why="after carol stalled and recovered")

        alice.manager.pause_request()
        bob.manager.seek_request(60.0)
        self.assertTogether(net, at=60.0, why="after a seek while paused")

        bob.manager.play_request()
        self.assertTogether(net, playing=True, at=60.0, why="at the end")

    def test_the_whole_sequence_is_cheap(self):
        """A sanity bound on the conversation as a whole: if the count here
        creeps up, some exchange has started echoing."""
        net = self.network(members=("alice", "bob", "carol"))
        net.members["alice"].manager.pause_request()
        net.members["bob"].manager.play_request()
        net.members["carol"].manager.seek_request(1200.0)
        self.assertLess(net.steps, 60,
                        "three actions across three members cost %d messages"
                        % net.steps)


if __name__ == "__main__":
    unittest.main()
