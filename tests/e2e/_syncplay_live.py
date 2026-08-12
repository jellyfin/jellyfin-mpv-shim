"""Two real SyncPlay clients on one real group, against a real server.

`tests/test_syncplay_e2e.py` asks the same questions against a modelled
server, and that suite is the one to reach for first: it is deterministic, it
runs in fifty milliseconds, and it can force states a live server only reaches
by luck. What it cannot do is be *wrong about the server*. It is a port, and a
port is a belief about someone else's code -- a belief that has already been
wrong here twice (a Buffer that broadcast nothing, an ignore-wait resume path
that sent the wrong command).

So this is the other half: the same convergence property, over real
websockets, with the real server deciding. If the two suites ever disagree,
the model is what is wrong.

**No mpv.** SyncPlay drives a player through a handful of calls
(`play`/`seek`/`set_paused`/`get_time`/`get_video`) and `LivePlayer`
implements exactly those, with a clock that really runs so the position it
reports to the server is honest. Nothing is monkeypatched onto the manager --
what is faked is the player, which is the seam SyncPlay was written against.
That keeps this in the contract tier: no display, no backend, seconds.

**What is real:** both websockets, both sessions, the group, every command,
and `Media` -- a play queue update builds one against the live server, so the
DTO round trip is exercised rather than described.

Timing is real too, which changes what an assertion can say. The modelled
suite compares positions to the group's, because it can read it; here the
group's position is not exposed by any endpoint, so tests compare members to
each other *and* to the value the test itself commanded. Both halves matter:
agreeing with each other is not synchronisation if they agree on the wrong
frame.
"""

import time
import uuid

from tests.e2e import _e2e

TICKS = 10_000_000

#: Real websocket round trips, so "the same frame" has slack in it. Measured
#: divergence between two clients playing for six seconds is under a
#: millisecond; a second is a ceiling that a genuine desync blows through
#: while a slow round trip does not.
TOLERANCE = 1.0


class LivePlayer:
    """The part of PlayerManager that SyncPlay actually drives.

    The clock runs, which is the point: the client reports its position to the
    server on every Ready, and a player frozen at zero gets "session got lost
    in time" corrections forever rather than converging.
    """

    def __init__(self):
        self.menu = type("Menu", (), {"is_menu_shown": False})()
        self.speed = 1.0
        self.video = None
        self.syncplay = None        # set by LiveMember; see play()
        self._paused = True
        self._base = 0.0
        self._since = time.monotonic()
        # Records, for tests that want the conversation and not the outcome.
        self.seeks = []
        self.messages = []
        self.stopped = []
        self.plays = []
        #: (owner, semantics) per claim_keys call; None semantics is a
        #: release. See claim_keys below.
        self.key_claims = []
        self.syncplay_notices = 0

    # -- the clock
    def _now(self):
        if self._paused:
            return self._base
        return self._base + (time.monotonic() - self._since) * self.speed

    def get_time(self):
        return self._now()

    @property
    def position(self):
        return self._now()

    @property
    def paused(self):
        return self._paused

    @property
    def playing(self):
        return not self._paused

    # -- what SyncPlay calls
    def set_paused(self, paused, *a):
        self._base = self._now()
        self._since = time.monotonic()
        self._paused = bool(paused)

    def seek(self, offset, **kw):
        self.seeks.append(offset)
        self._base = offset
        self._since = time.monotonic()

    def get_speed(self):
        return self.speed

    def set_speed(self, speed):
        self._base = self._now()
        self._since = time.monotonic()
        self.speed = speed

    def show_text(self, text, *a, **kw):
        self.messages.append(text)

    def get_video(self):
        return self.video

    def is_not_paused(self):
        return not self._paused

    def get_current_client(self):
        return None

    def stop(self, leave_group=True):
        self.stopped.append(leave_group)
        self.set_paused(True)

    def put_task(self, func, *args):
        func(*args)

    def has_video(self):
        return self.video is not None

    def send_timeline(self):
        pass

    def timeline_handle(self):
        pass

    def upd_player_hide(self):
        pass

    def play(self, video, offset=None, **kw):
        """``SyncPlayManager._play_video`` lands here.

        The tail matters as much as the load: a real ``_play_media`` finishes
        by calling ``syncplay.play_done()``, which pauses locally and reports
        Ready. Without it the group sits in Waiting for a client that has in
        fact loaded, which looks exactly like the bug this suite is for.
        """
        self.plays.append((video, offset))
        self.video = video
        self.seek(offset or 0.0)
        self.set_paused(True)
        if self.syncplay is not None:
            self.syncplay.play_done()

    def reset(self):
        self.video = None
        self._paused = True
        self._base = 0.0
        self._since = time.monotonic()
        del self.seeks[:], self.messages[:], self.stopped[:], self.plays[:]

    def __repr__(self):
        return "<LivePlayer at %.3fs %s>" % (
            self.position, "paused" if self._paused else "playing")

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


class LiveMember:
    """One person: a real session, a real websocket, a real manager."""

    def __init__(self, label, account, device_id):
        from jellyfin_mpv_shim.syncplay import SyncPlayManager

        self.label = label
        self.account = account
        self.session = _e2e.Session(account, device_id=device_id,
                                    websocket=True)
        self.player = LivePlayer()
        self.manager = SyncPlayManager(self.player)
        self.manager.client = self.session.client
        self.player.syncplay = self.manager
        self.session.listeners.append(self._on_event)

    def _on_event(self, event_name, data):
        """What `event_handler` does with the two SyncPlay events."""
        if event_name == "SyncPlayCommand":
            self.manager.client = self.session.client
            self.manager.process_command(dict(data))
        elif event_name == "SyncPlayGroupUpdate":
            self.manager.client = self.session.client
            self.manager.process_group_update(dict(data))

    # -- readings the assertions are made of
    @property
    def position(self):
        return self.player.position

    @property
    def playing(self):
        return self.player.playing

    @property
    def paused(self):
        return self.player.paused

    @property
    def api(self):
        return self.session.client.jellyfin

    def reset(self):
        """Forget the previous test's playback.

        Leaving a group does not stop a player, so without this a member
        starts the next test still holding the last one's video and pause
        state -- and every "wait until it has loaded / is playing" is then
        satisfied by state from before the action under test. That is not a
        slow test, it is a test that measures nothing, and it read as a
        flaky client."""
        self.player.reset()

    def leave(self):
        # Only when there is something to leave: SyncPlay/Leave answers 403
        # when you are not in a group, and a blanket leave in teardown made
        # every run end with two Forbidden lines that meant nothing.
        if not self.manager.in_group():
            return
        try:
            self.api.leave_sync_play()
        except Exception:
            pass

    def stop(self):
        self.leave()
        try:
            self.session.stop()
        except Exception:
            pass

    def __repr__(self):
        return "<%s %r>" % (self.label, self.player)


class LiveGroup:
    """A group with real members on it, and the cleanup that keeps the QA
    server tidy between runs."""

    def __init__(self, members):
        self.members = {m.label: m for m in members}
        self.group_id = None

    @classmethod
    def build(cls, *specs, **kw):
        """`specs` are (label, account) pairs. Device ids carry
        `_e2e.DEVICE_PREFIX` so `purge_devices` can clean up after a crashed
        run -- an id built any other way is permanent litter. `tag`
        distinguishes one test class's devices from another's."""
        tag = kw.pop("tag", "a")
        assert not kw, kw
        members = [LiveMember(label, account,
                              "%ssp-%s-%s" % (_e2e.DEVICE_PREFIX, tag, label))
                   for label, account in specs]
        return cls(members)

    def __getitem__(self, label):
        return self.members[label]

    def create(self, owner, name=None):
        """`owner` creates a group and everybody else joins it."""
        member = self.members[owner]
        member.api.new_sync_play_v2(name or ("jms-e2e " + uuid.uuid4().hex[:8]))
        if not wait_until(lambda: member.manager.in_group(), timeout=15):
            raise AssertionError(
                "%s never received GroupJoined after creating a group; "
                "events were %r" % (owner, [n for n, _ in member.session.events]))
        self.group_id = member.manager.current_group

        for label, other in self.members.items():
            if label == owner:
                continue
            other.api.join_sync_play(self.group_id)
            if not wait_until(lambda o=other: o.manager.in_group(), timeout=15):
                raise AssertionError("%s never joined %s" % (label, self.group_id))
        return self.group_id

    def start(self, item_ids, position_ticks=0, by=None):
        """Put something on. Returns once every member has loaded *this* queue.

        Counted against a mark taken before the request, never against "does
        this member have a video": a member that still holds the previous
        test's video satisfies that instantly, and the test then runs while
        the new PlayQueue is still in flight. The symptom was a member who
        had just been halted turning up un-halted a moment later -- which is
        correct behaviour (a NewPlaylist re-attaches a halted member) racing
        a test that had not actually waited for anything.
        """
        owner = self.members[by or next(iter(self.members))]
        marks = {label: len(m.player.plays) for label, m in self.members.items()}
        owner.api.reset_queue_sync_play(list(item_ids), 0, position_ticks)
        if not wait_until(lambda: all(len(m.player.plays) > marks[label]
                                      for label, m in self.members.items()),
                          timeout=20):
            raise AssertionError(
                "not every member loaded the queue this test set: %r"
                % (self.members,))

    def participants(self, member=None):
        """The group's participants as the server reports them."""
        who = member or next(iter(self.members.values()))
        for group in who.api.get_sync_play() or []:
            if group.get("GroupId") == self.group_id:
                return group.get("Participants") or []
        return []

    def reset(self):
        for member in self.members.values():
            member.reset()

    def close(self):
        for member in self.members.values():
            member.stop()


def wait_until(predicate, timeout=10, interval=0.05):
    """Poll until true. Everything here is asynchronous -- a command is a
    websocket round trip and the server deliberately schedules unpauses a
    little in the future -- so every assertion about a *change* has to be a
    wait, and every timeout has to be long enough to survive a slow box."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    try:
        return bool(predicate())
    except Exception:
        return False


def settled(members, timeout=10):
    """Wait for every member to agree on whether it is playing.

    Not a position check: the members are constantly moving while playing, so
    "settled" means the pause state has stopped changing, and the position
    assertions come after."""
    def agreed():
        states = {m.playing for m in members}
        return len(states) == 1
    return wait_until(agreed, timeout=timeout)
