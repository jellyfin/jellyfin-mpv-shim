"""Several real SyncPlay clients on one group, with the bus between them.

``_syncplay_server`` models the server and ``bind`` puts *one* real
``SyncPlayManager`` on it; everyone else in the group is a bare session
record. That is enough to ask "does the client answer the protocol", and not
enough to ask the question SyncPlay actually exists to answer: **do two
people end up watching the same frame at the same time?**

So this module seats two or more real managers on one group and routes every
message the server emits to every client it is addressed to. Each client has
its own player, its own timesync and its own view of the world, and nothing
in the client code knows it is in a test. What comes out is the property
that matters -- convergence -- rather than a list of calls.

**Delivery is queued, not re-entrant.** A client answering a broadcast
usually sends something itself (a Seek makes everyone report Ready, and the
last Ready unpauses the group), so delivering inline would nest one client's
reaction inside another's send, in an order no real network produces. The bus
appends to a queue and drains it in order at the top level instead, which is
both closer to the real thing and a place to put a bound: ``MAX_STEPS``
turns "the group never settles" into a test failure with a message rather
than a hung suite or a stack overflow. That bound is itself an assertion --
a protocol that needs unbounded chatter to converge has not converged.

**The clocks are honest here** (``skew_seconds=0``): these tests compare
positions between clients and against the group, so a skew that pushes every
client past the group would make the numbers meaningless. Commands still
apply inline, because a ``When`` stamped a moment ago is already in the past.

Nothing here spawns a thread or touches the network, so it belongs in the
fast suite.
"""

import collections

from tests._syncplay_server import (
    PLAYING,
    FakeClient,
    FakeJellyfinApi,
    FakeTimesync,
    SyncPlayGroup,
)

TICKS = 10_000_000


class FakeMenu:
    is_menu_shown = False


class FakeQueue:
    """The client's playlist. ``replace_queue`` answering None means "same
    item", which is what a queue update that only re-states the current
    item should do -- the client then reports Ready instead of reloading."""

    def __init__(self, playlist_item_id):
        self.seq = 0
        self.queue = [{"PlaylistItemId": playlist_item_id, "ItemId": "item-1"}]
        # Derived, and carried here because a stand-in that omits the field an
        # invariant is about cannot fail that invariant: the real Media once
        # left these frozen across a group queue update, and no suite that
        # drives upd_queue could see it.
        self.has_next = False
        self.has_prev = False

    def replace_queue(self, items, index):
        self.queue = list(items) or self.queue
        self.seq = index if index is not None and index >= 0 else 0
        self.has_next = self.seq < len(self.queue) - 1
        self.has_prev = self.seq > 0
        return None


class FakeVideo:
    def __init__(self, queue):
        self.parent = queue

    def get_playlist_id(self):
        return self.parent.queue[self.parent.seq]["PlaylistItemId"]


class FakePlayer:
    """One member's player. Position and pause are real state that the
    SyncPlay code drives, which is the whole point: the assertions read them
    back and compare members to each other."""

    def __init__(self, playlist_item_id="pl-1", position=0.0):
        self.menu = FakeMenu()
        self.speed = 1.0
        self.paused = True
        self.position = position
        self.seeks = []
        self.messages = []
        self.stopped = []
        self.video = FakeVideo(FakeQueue(playlist_item_id))
        #: (owner, semantics) per claim_keys call; None semantics is a
        #: release. See claim_keys below.
        self.key_claims = []
        self.syncplay_notices = 0

    # -- what SyncPlayManager drives
    def get_speed(self):
        return self.speed

    def set_speed(self, speed):
        self.speed = speed

    def set_paused(self, paused, *a):
        self.paused = paused

    def seek(self, offset, **kw):
        self.seeks.append(offset)
        self.position = offset

    def show_text(self, text, *a, **kw):
        self.messages.append(text)

    def get_time(self):
        return self.position

    def get_video(self):
        return self.video

    def is_not_paused(self):
        return not self.paused

    def get_current_client(self):
        return None

    def stop(self, leave_group=True):
        self.stopped.append(leave_group)
        self.paused = True

    def put_task(self, func, *args):
        func(*args)

    # The rest of the contract. Never reached by these tests, and present
    # anyway: a stand-in missing a method the manager can call does not make
    # that path untested, it makes it pass by raising somewhere nobody looks.
    # Pinned by tests/test_syncplay_player_contract.py.
    def play(self, video, offset=None, **kwargs):
        self.video = video

    def has_video(self):
        return self.get_video() is not None

    def send_timeline(self):
        pass

    def timeline_handle(self):
        pass

    def upd_player_hide(self):
        pass

    # --- the key claim (tests/test_syncplay_player_contract.py) ---
    #
    # Reached through `getattr(self.playerManager, "...", None)` in
    # syncplay.py, so until the contract extractor learned that spelling
    # these three were invisible: no stand-in had them, getattr answered
    # None on every test player, and replacing both `_claim_keys` call
    # sites with `pass` -- deleting the feature -- left 34 tests green.
    #
    # Recorders rather than no-ops, so a test can assert the claim
    # HAPPENED and not merely that it did not raise.
    def claim_keys(self, owner, semantics=None):
        self.key_claims.append((owner, semantics))

    def on_syncplay_change(self):
        self.syncplay_notices += 1

    def notify_syncplay(self, *args, **kwargs):
        self.syncplay_notices += 1


class Member:
    """One person in the group: their manager, their player, their wire."""

    def __init__(self, name, manager, player, api):
        self.name = name
        self.manager = manager
        self.player = player
        self.api = api

    # Reading a member's state is what the assertions are made of, so give
    # those readings names rather than reaching into the player everywhere.
    @property
    def position(self):
        return self.player.position

    @property
    def paused(self):
        return self.player.paused

    @property
    def playing(self):
        return not self.player.paused

    def __repr__(self):
        return "<Member %s at %.3fs %s>" % (
            self.name, self.position, "paused" if self.paused else "playing")


class SyncPlayNetwork:
    """One group, N real clients, one message bus."""

    #: A generous ceiling on messages per user action. Real exchanges here
    #: settle in well under ten; this only has to be low enough that a loop
    #: fails the test rather than running until something else breaks.
    MAX_STEPS = 200

    def __init__(self, state=PLAYING, position_ticks=0, playing_item_id="pl-1"):
        self.group = SyncPlayGroup(state=state, position_ticks=position_ticks,
                                   playing_item_id=playing_item_id)
        self.members = {}
        self._pending = collections.deque()
        self._draining = False
        #: Every (session, kind, payload) the bus handed to a client, for
        #: tests that want to assert on the conversation and not just its
        #: outcome.
        self.delivered = []
        self.steps = 0

    # -- seating ----------------------------------------------------------

    def seat(self, name, position=None):
        """Add a member who is already watching along.

        The quiet seat: the group is told about the session without running
        the join handshake, which is how a test starts from "three people are
        watching this" without paying for the arrival of each. Use
        :meth:`join` to test arriving.
        """
        from jellyfin_mpv_shim.syncplay import SyncPlayManager

        if position is None:
            position = self.group.position_ticks / TICKS
        player = FakePlayer(self.group.playing_item_id, position=position)
        player.paused = self.group.state != PLAYING
        manager = SyncPlayManager(player)
        timesync = FakeTimesync(skew_seconds=0)
        api = FakeJellyfinApi(self.group, name, self._deliver)
        manager.client = FakeClient(api, timesync)
        manager.timesync = timesync
        manager.enable_sync_play(from_server=True)
        timesync.force_update()          # flips `ready`, drains any queued cmd
        manager.current_group = self.group.group_id

        session = self.group.add_session(name)
        session.buffering = False
        member = Member(name, manager, player, api)
        self.members[name] = member
        return member

    def join(self, name):
        """Add a member the way one actually arrives: through SyncPlay/Join,
        which puts the group into Waiting until the newcomer reports Ready."""
        member = self.seat(name)
        # add_session in seat() gave them a chair; session_joined is the
        # server's arrival handling, and it is what sends them the queue.
        self._deliver(self.group.session_joined(name))
        return member

    # -- the bus ----------------------------------------------------------

    def _deliver(self, entries):
        """Queue every emitted message, then drain in order.

        Called by each client's api after its request, and by :meth:`join`.
        A nested call (a client reacting mid-drain) only enqueues."""
        self._pending.extend(entries)
        if self._draining:
            return
        self._draining = True
        try:
            steps = 0
            while self._pending:
                steps += 1
                self.steps += 1
                if steps > self.MAX_STEPS:
                    raise AssertionError(
                        "the group did not settle: %d messages from one "
                        "action and still going. Last few: %s"
                        % (steps, self.delivered[-6:]))
                kind, targets, payload = self._pending.popleft()
                for session_id in targets:
                    member = self.members.get(session_id)
                    if member is None:
                        continue        # a bare session, or one that left
                    self.delivered.append((session_id, kind,
                                           payload.get("Command")
                                           or payload.get("Type")))
                    if kind == "command":
                        member.manager.process_command(dict(payload))
                    else:
                        member.manager.process_group_update(dict(payload))
        finally:
            self._draining = False

    # -- reading the room -------------------------------------------------

    @property
    def group_position(self):
        return self.group.position_ticks / TICKS

    def positions(self):
        return {name: m.position for name, m in self.members.items()}

    def paused(self):
        return {name: m.paused for name, m in self.members.items()}
