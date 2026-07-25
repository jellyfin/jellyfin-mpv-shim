"""SyncPlay protocol conformance: does the client keep the group moving?

The existing SyncPlay tests all check *client-internal* state -- generations,
teardown, scheduled-command supersession. None of them models the server, so
the client could stop answering the protocol entirely and the suite would stay
green. It did, and that is how a port made from jellyfin-web's client logic
around 2019 drifted from the server without anyone noticing.

These drive a real ``SyncPlayManager`` against ``tests/_syncplay_server.py``,
a port of the server's group state machine (see that module for what it is a
port *of*, and where it is deliberately weaker).

The rule the whole protocol hangs off: a group enters ``Waiting`` on a seek,
a buffer report, a play, or anyone joining -- and leaves it only when *every*
session has sent ``Ready``. A client that does not answer stops the group for
everyone, and the failure is silent on both ends.
"""

import sys
import unittest
from datetime import datetime, timedelta

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.syncplay import SyncPlayManager  # noqa: E402
from tests._syncplay_server import (  # noqa: E402
    PAUSED,
    PLAYING,
    WAITING,
    SyncPlayGroup,
    bind,
)


class FakeMenu:
    is_menu_shown = False


class FakePlayer:
    """Enough PlayerManager for the SyncPlay paths."""

    def __init__(self):
        self.menu = FakeMenu()
        self.speed = 1.0
        self.paused = None
        self.seeks = []
        self.messages = []

    def get_speed(self):
        return self.speed

    def set_speed(self, speed):
        self.speed = speed

    def set_paused(self, paused, *a):
        self.paused = paused

    def seek(self, offset, **kw):
        self.seeks.append(offset)

    def show_text(self, text, *a, **kw):
        self.messages.append(text)

    def get_current_client(self):
        return None

    def get_time(self):
        return 0.0


class ProtocolCase(unittest.TestCase):
    def group_and_client(self, state=PLAYING, **kw):
        group = SyncPlayGroup(state=state, **kw)
        # A second member, so "the group is waiting for someone" is
        # distinguishable from "the group is empty".
        other = group.add_session("other-session")
        other.buffering = False
        sp = SyncPlayManager(FakePlayer())
        api = bind(sp, group)
        group.sessions["session-under-test"].buffering = False
        group.state = state
        return group, sp, api

    def assertReadyWasSent(self, api, why):
        self.assertIn("Ready", api.kinds(), why)


class TestTheHandshakeAfterASeek(ProtocolCase):
    """A Seek command puts every session back into buffering.

    ``WaitingGroupState.HandleRequest(SeekGroupRequest)`` broadcasts Seek and
    then calls ``SetAllBuffering(true)``. Until this client answers Ready the
    group cannot resume -- for anybody.
    """

    @unittest.expectedFailure      # finding 2 in docs/SYNCPLAY_FINDINGS.md
    def test_the_client_answers_ready_after_a_group_seek(self):
        group, sp, api = self.group_and_client()
        group.request("Seek", "other-session", position_ticks=60 * 10_000_000)
        # Deliver the resulting Seek command to the client under test.
        for kind, targets, payload in group.outbox:
            if kind == "command" and "session-under-test" in targets:
                sp.process_command(dict(payload))
        self.assertReadyWasSent(
            api,
            "the client never reported Ready after a group Seek, so the "
            "group stays in Waiting until someone force-unpauses it")

    @unittest.expectedFailure      # finding 2 in docs/SYNCPLAY_FINDINGS.md
    def test_the_group_is_not_left_waiting_on_us(self):
        group, sp, api = self.group_and_client()
        for kind, targets, payload in group.request(
                "Seek", "other-session", position_ticks=30 * 10_000_000):
            if kind == "command" and "session-under-test" in targets:
                sp.process_command(dict(payload))
        group.request("Ready", "other-session", position_ticks=30 * 10_000_000,
                      is_playing=True, playlist_item_id=group.playing_item_id)
        self.assertNotIn(
            "session-under-test", group.waiting_on(),
            "the group is still waiting on this client after a seek")


class TestTheStopCommand(ProtocolCase):
    """``SendCommandType.Stop`` is one of the four commands the server sends.

    Reached when any member stops the group, when the queue empties, and when
    a session joins an idle group (``IdleGroupState.cs``).
    """

    @unittest.expectedFailure      # finding 3 in docs/SYNCPLAY_FINDINGS.md
    def test_a_group_stop_is_handled(self):
        group, sp, api = self.group_and_client()
        emitted = group.request("Stop", "other-session")
        handled = []
        sp.playerManager.stop = lambda *a, **k: handled.append("stop")
        for kind, targets, payload in emitted:
            if kind == "command" and "session-under-test" in targets:
                sp.process_command(dict(payload))
        self.assertTrue(
            handled,
            "another member stopped the group and this client kept playing "
            "(process_command logs 'Command Stop is unknown')")


class TestBufferingIsReported(unittest.TestCase):
    """The server has a Buffer request so the group pauses for a stalled
    member. The client only ever calls it from mpv's ``seeking`` property --
    nothing observes ``paused-for-cache``, so a cache underrun desyncs this
    client silently and it is then yanked by SkipToSync."""

    @unittest.expectedFailure      # finding 4 in docs/SYNCPLAY_FINDINGS.md
    def test_the_player_observes_a_cache_stall(self):
        import ast
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "jellyfin_mpv_shim", "player.py")).read()
        observed = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if name in ("_observe", "property_observer", "bind_property_observer"):
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            observed.add(arg.value)
        self.assertTrue(
            observed & {"paused-for-cache", "core-idle"},
            "nothing observes a cache stall, so SyncPlay is never told this "
            "client is buffering; observed properties were %s" % sorted(observed))


class TestTheGroupStateMachineItself(ProtocolCase):
    """Guards on the mock, so a wrong mock cannot quietly pass the rest."""

    def test_a_seek_puts_everyone_back_to_buffering(self):
        group, sp, api = self.group_and_client()
        group.request("Seek", "other-session", position_ticks=5)
        self.assertEqual(group.state, WAITING)
        self.assertEqual(sorted(group.waiting_on()),
                         ["other-session", "session-under-test"])

    def test_the_group_resumes_only_when_everyone_is_ready(self):
        group, sp, api = self.group_and_client()
        group.request("Seek", "other-session", position_ticks=0)
        group.request("Ready", "other-session", position_ticks=0,
                      is_playing=True, playlist_item_id=group.playing_item_id)
        self.assertEqual(group.state, WAITING, "resumed while one was buffering")
        group.request("Ready", "session-under-test", position_ticks=0,
                      is_playing=True, playlist_item_id=group.playing_item_id)
        self.assertEqual(group.state, PLAYING)

    def test_a_ready_for_the_wrong_item_keeps_the_session_buffering(self):
        group, sp, api = self.group_and_client()
        group.request("Seek", "other-session", position_ticks=0)
        group.request("Ready", "session-under-test", position_ticks=0,
                      is_playing=True, playlist_item_id="some-other-item")
        self.assertIn("session-under-test", group.waiting_on())

    def test_pause_and_unpause_move_between_playing_and_paused(self):
        group, sp, api = self.group_and_client()
        group.request("Pause", "other-session")
        self.assertEqual(group.state, PAUSED)
        group.request("Unpause", "other-session")
        self.assertEqual(group.state, PLAYING)

    def test_a_same_state_request_is_a_resync_to_that_session_only(self):
        """The server's only correction channel: an identical command sent
        to one session."""
        group, sp, api = self.group_and_client()
        emitted = group.request("Unpause", "other-session")
        self.assertEqual([e[1] for e in emitted if e[0] == "command"],
                         [["other-session"]])


class TestTheClientAcceptsTheServersResync(ProtocolCase):
    """The server corrects a drifted client by re-sending the *same* command.

    ``PlayingGroupState``/``PausedGroupState`` build it with the unchanged
    ``LastActivity`` and ``PositionTicks``, so it is byte-identical to the
    last broadcast -- and the client drops duplicates.

    **Marked expected-failure but not yet confirmed as a bug.** jellyfin-web
    has the same duplicate filter, so either its resync is equally useless or
    it distinguishes them in a way this port missed. Check jellyfin-web before
    changing anything here; the test exists to make the behaviour visible, not
    to assert a verdict.
    """

    @unittest.expectedFailure      # finding 7 -- UNCONFIRMED, see below
    def test_a_resend_of_the_current_state_is_acted_on(self):
        group, sp, api = self.group_and_client(state=PAUSED)
        sp.enabled_at = datetime.utcnow() - timedelta(hours=1)
        first = [p for k, t, p in group.request("Pause", "other-session")
                 if k == "command"]
        self.assertTrue(first)
        sp.process_command(dict(first[0]))
        sp.playerManager.paused = None
        # Same command again -- what the server sends a client it thinks is lost.
        sp.process_command(dict(first[0]))
        self.assertIsNotNone(
            sp.playerManager.paused,
            "the duplicate filter dropped the server's resync, which is the "
            "only mechanism it has for correcting a drifted client")


if __name__ == "__main__":
    unittest.main()
