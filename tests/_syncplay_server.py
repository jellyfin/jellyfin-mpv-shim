"""A fake Jellyfin SyncPlay server: the group state machine, ported from source.

**Why a mock rather than a real server.** A live Jellyfin tests this client
against one server build and cannot be made to hold an awkward state on
demand. What we need to check is conformance: does the client keep the group
*moving*? That means forcing a group to sit in ``Waiting``, or re-sending a
byte-identical command, or reporting a playlist item the client is not on --
states a real server only reaches by luck. Those are one line each here.

**What this is a port of**, so it can be checked rather than trusted. Read
from the Jellyfin source, ``MediaBrowser.Controller/SyncPlay/GroupStates/``:

* ``IdleGroupState.cs``    -- Idle
* ``WaitingGroupState.cs`` -- Waiting  (the one that matters)
* ``PlayingGroupState.cs`` -- Playing
* ``PausedGroupState.cs``  -- Paused

and the wire enums from ``MediaBrowser.Model/SyncPlay/``: ``GroupStateType``
(Idle, Waiting, Paused, Playing), ``SendCommandType`` (Unpause, Pause, Stop,
Seek -- **four**, and Stop is one of them), ``GroupUpdateType`` and
``PlaybackRequestType``. Enums serialize as strings, so the client comparing
against ``"Unpause"`` is correct.

**The invariant everything hangs off:** a group enters ``Waiting`` on Play,
Seek, Buffer, SetPlaylistItem, NextItem, PreviousItem and on any session
joining a Playing/Paused group -- and it leaves ``Waiting`` only when *every*
non-ignored session has sent ``Ready``. A client that does not answer leaves
the whole group stopped. ``WaitingGroupState.cs`` calls ``SetAllBuffering(true)``
at lines 160, 185, 318 and ``SetBuffering(session, true)`` at 84.

**Deliberately simplified**, and each of these is a place the mock is weaker
than the server rather than different from it:

* No latency compensation. ``GetHighestPing`` is treated as zero, so ``When``
  is always ``LastActivity``. The client's own scheduling is what tests here
  care about, not the server's fudge factor.
* ``Ping``, ``IgnoreWait``, ``SetRepeatMode``, ``SetShuffleMode``,
  ``MovePlaylistItem`` and ``Queue`` are accepted and recorded but change no
  state.
* One group, and the session under test is always in it.

Times go out in Jellyfin's format -- ISO with **seven** fractional digits and
a ``Z`` -- because the client parses them with ``time[:-2]``, which assumes
exactly that shape.
"""

import datetime

IDLE, WAITING, PAUSED, PLAYING = "Idle", "Waiting", "Paused", "Playing"

#: SendCommandType. Four values; the client handles three of them.
COMMANDS = ("Unpause", "Pause", "Stop", "Seek")

TICKS_PER_SECOND = 10_000_000

#: Group.MaxPlaybackOffset default, in ticks (500 ms).
MAX_PLAYBACK_OFFSET_TICKS = 500 * 10_000


def jf_time(when):
    """Serialize like System.Text.Json does a DateTime: 7 fractional digits, Z."""
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + "%07d" % (when.microsecond * 10) + "Z"


class Session:
    def __init__(self, session_id):
        self.id = session_id
        #: Sessions start buffering; the server waits for a Ready from each.
        self.buffering = True
        #: ``Group.SetIgnoreGroupWait``. A member who has stopped watching but
        #: not left: still in the group, no longer held for. This is what a
        #: halt (SyncPlay/SetIgnoreWait) sets, and it is the whole reason a
        #: halt is not just "stop sending Ready" -- without it the group waits
        #: on us forever.
        self.ignore_wait = False

    def __repr__(self):
        return "<Session %s buffering=%s ignore_wait=%s>" % (
            self.id, self.buffering, self.ignore_wait)


class SyncPlayGroup:
    """One SyncPlay group. Call :meth:`request`; read :attr:`outbox`."""

    def __init__(self, state=IDLE, position_ticks=0, playing_item_id="pl-1",
                 queue=None):
        self.group_id = "group-1"
        self.state = state
        self.position_ticks = position_ticks
        self.playing_item_id = playing_item_id
        self.queue = list(queue or [{"PlaylistItemId": playing_item_id,
                                     "ItemId": "item-1"}])
        self.playing_item_index = 0
        self.sessions = {}
        self.last_activity = datetime.datetime.utcnow()
        #: Set when entering Waiting: True from Playing, False from Paused.
        self.resume_playing = True
        #: Every (kind, targets, payload) the server emitted, in order.
        self.outbox = []
        #: Every request it received, for asserting on what the client sent.
        self.received = []
        self._last_emitted = None

    # -- session bookkeeping ----------------------------------------------

    def add_session(self, session_id):
        session = self.sessions[session_id] = Session(session_id)
        return session

    def set_buffering(self, session_id, value):
        self.sessions[session_id].buffering = value

    def set_all_buffering(self, value):
        for s in self.sessions.values():
            s.buffering = value

    def is_buffering(self):
        """True while any session that is still being waited for has not
        reported Ready (``Group.IsBuffering``: ignore-wait members excluded)."""
        return any(s.buffering and not s.ignore_wait
                   for s in self.sessions.values())

    def waiting_on(self):
        return sorted(s.id for s in self.sessions.values()
                      if s.buffering and not s.ignore_wait)

    def set_ignore_wait(self, session_id, value):
        self.sessions[session_id].ignore_wait = value

    # -- emitting ----------------------------------------------------------

    def _targets(self, broadcast, session_id):
        if broadcast == "CurrentSession":
            return [session_id]
        if broadcast == "AllExceptCurrentSession":
            return [i for i in self.sessions if i != session_id]
        if broadcast == "AllReady":
            return [i for i, s in self.sessions.items() if not s.buffering]
        return list(self.sessions)          # AllGroup

    def _emitted_at(self):
        """Strictly increasing, because EmittedAt is what tells a resync apart
        from the broadcast it repeats. Two commands in the same microsecond
        would be genuinely indistinguishable and make tests flaky."""
        now = datetime.datetime.utcnow()
        if self._last_emitted is not None and now <= self._last_emitted:
            now = self._last_emitted + datetime.timedelta(microseconds=1)
        self._last_emitted = now
        return now

    def send_command(self, session_id, broadcast, command):
        assert command in COMMANDS, command
        now = self._emitted_at()
        payload = {
            "GroupId": self.group_id,
            "PlaylistItemId": self.playing_item_id,
            "When": jf_time(self.last_activity),
            "Command": command,
            "PositionTicks": self.position_ticks,
            "EmittedAt": jf_time(now),
        }
        self.outbox.append(("command", self._targets(broadcast, session_id), payload))

    def send_update(self, session_id, broadcast, update_type, data=None):
        payload = {"GroupId": self.group_id, "Type": update_type, "Data": data}
        self.outbox.append(("update", self._targets(broadcast, session_id), payload))

    def _state_update(self, session_id, reason):
        self.send_update(session_id, "AllGroup", "StateUpdate",
                         {"State": self.state, "Reason": reason})

    def _play_queue_update(self, session_id, broadcast, reason):
        self.send_update(session_id, broadcast, "PlayQueue", {
            "Reason": reason,
            "LastUpdate": jf_time(datetime.datetime.utcnow()),
            "Playlist": self.queue,
            "PlayingItemIndex": self.playing_item_index,
            "StartPositionTicks": self.position_ticks,
            "IsPlaying": self.state == PLAYING,
        })

    # -- dispatch ----------------------------------------------------------

    def request(self, kind, session_id, **kw):
        """Handle one PlaybackRequestType. Returns the newly emitted entries."""
        if session_id not in self.sessions:
            self.add_session(session_id)
        self.received.append((kind, session_id, kw))
        mark = len(self.outbox)
        prev = self.state
        if kind == "IgnoreWait":
            # Every state handles this one the same way
            # (AbstractGroupState.HandleRequest(IgnoreWaitGroupRequest)): set
            # the flag, and if that was the last session the group was held
            # up by, stop waiting.
            self.set_ignore_wait(session_id, kw.get("ignore_wait", True))
            if self.state == WAITING and not self.is_buffering():
                # "Client, that was buffering, stopped following playback."
                # Resuming broadcasts an Unpause; returning to Paused just
                # changes state, and sends nothing.
                if self.resume_playing:
                    self.state = PLAYING
                    self.last_activity = datetime.datetime.utcnow()
                    self.send_command(session_id, "AllGroup", "Unpause")
                else:
                    self.state = PAUSED
            return self.outbox[mark:]
        handler = getattr(self, "_%s_%s" % (self.state.lower(), kind.lower()), None)
        if handler is None:
            handler = getattr(self, "_%s_default" % self.state.lower())
            handler(session_id, kind, prev, **kw)
        else:
            handler(session_id, prev, **kw)
        return self.outbox[mark:]

    def session_joined(self, session_id):
        self.add_session(session_id)
        mark = len(self.outbox)
        if self.state == IDLE:
            # IdleGroupState.SessionJoined -> SendStopCommand(prev == Idle).
            self.send_command(session_id, "CurrentSession", "Stop")
        else:
            # Playing/Paused -> WaitingGroupState.SessionJoined.
            self.resume_playing = self.state == PLAYING
            self.state = WAITING
            self.set_buffering(session_id, True)
            self._play_queue_update(session_id, "CurrentSession", "SetCurrentItem")
            self.send_command(session_id, "AllReady", "Pause")
        return self.outbox[mark:]

    def session_leaving(self, session_id):
        mark = len(self.outbox)
        self.sessions.pop(session_id, None)
        if self.state == WAITING and not self.is_buffering():
            self.state = PLAYING if self.resume_playing else PAUSED
            self.send_command(session_id, "AllGroup",
                              "Unpause" if self.resume_playing else "Pause")
        return self.outbox[mark:]

    def _enter_waiting(self, prev):
        if prev == PLAYING:
            self.resume_playing = True
        elif prev == PAUSED:
            self.resume_playing = False
        self.state = WAITING

    # -- Idle --------------------------------------------------------------

    def _idle_default(self, session_id, kind, prev, **kw):
        # Pause/Stop/Seek/Buffer/Ready all re-send Stop.
        self.send_command(session_id, "AllGroup" if prev != IDLE else "CurrentSession",
                          "Stop")

    def _idle_play(self, session_id, prev, **kw):
        self._enter_waiting(prev)
        self.set_all_buffering(True)

    def _idle_unpause(self, session_id, prev, **kw):
        self._enter_waiting(prev)
        self.set_all_buffering(True)

    # -- Playing -----------------------------------------------------------

    def _playing_default(self, session_id, kind, prev, **kw):
        pass

    def _playing_unpause(self, session_id, prev, **kw):
        if prev != PLAYING:
            self.last_activity = datetime.datetime.utcnow()
            self.send_command(session_id, "AllGroup", "Unpause")
            self._state_update(session_id, "Unpause")
        else:
            # "Client got lost, sending current state." -- byte-identical to
            # the last broadcast, which is the only resync channel there is.
            self.send_command(session_id, "CurrentSession", "Unpause")

    def _playing_pause(self, session_id, prev, **kw):
        self.state = PAUSED
        self._paused_pause(session_id, PLAYING, **kw)

    def _playing_stop(self, session_id, prev, **kw):
        self.state = IDLE
        self._idle_default(session_id, "Stop", PLAYING)

    def _playing_seek(self, session_id, prev, **kw):
        self._enter_waiting(PLAYING)
        self._waiting_seek(session_id, PLAYING, **kw)

    def _playing_buffer(self, session_id, prev, **kw):
        self._enter_waiting(PLAYING)
        self._waiting_buffer(session_id, PLAYING, **kw)

    def _playing_ready(self, session_id, prev, **kw):
        self.send_command(session_id, "CurrentSession", "Unpause")

    # -- Paused ------------------------------------------------------------

    def _paused_default(self, session_id, kind, prev, **kw):
        pass

    def _paused_pause(self, session_id, prev, **kw):
        if prev != PAUSED:
            now = datetime.datetime.utcnow()
            elapsed = (now - self.last_activity).total_seconds()
            self.last_activity = now
            self.position_ticks += max(int(elapsed * TICKS_PER_SECOND), 0)
            self.send_command(session_id, "AllGroup", "Pause")
            self._state_update(session_id, "Pause")
        else:
            self.send_command(session_id, "CurrentSession", "Pause")

    def _paused_unpause(self, session_id, prev, **kw):
        self.state = PLAYING
        self._playing_unpause(session_id, PAUSED, **kw)

    def _paused_stop(self, session_id, prev, **kw):
        self.state = IDLE
        self._idle_default(session_id, "Stop", PAUSED)

    def _paused_seek(self, session_id, prev, **kw):
        self._enter_waiting(PAUSED)
        self._waiting_seek(session_id, PAUSED, **kw)

    def _paused_buffer(self, session_id, prev, **kw):
        self._enter_waiting(PAUSED)
        self._waiting_buffer(session_id, PAUSED, **kw)

    def _paused_ready(self, session_id, prev, **kw):
        self.send_command(session_id, "CurrentSession", "Pause")

    # -- Waiting -----------------------------------------------------------

    def _waiting_default(self, session_id, kind, prev, **kw):
        pass

    def _waiting_play(self, session_id, prev, **kw):
        self.set_all_buffering(True)

    def _waiting_seek(self, session_id, prev, **kw):
        self.position_ticks = kw.get("position_ticks", self.position_ticks)
        self.last_activity = datetime.datetime.utcnow()
        self.send_command(session_id, "AllGroup", "Seek")
        # The whole point: everyone must report Ready again.
        self.set_all_buffering(True)
        self._state_update(session_id, "Seek")

    def _waiting_buffer(self, session_id, prev, **kw):
        """``WaitingGroupState.HandleRequest(BufferGroupRequest)``.

        This used to set the flag and send nothing, which quietly left the
        headline behaviour of the whole feature -- *the group pauses for a
        member who has stalled* -- unmodelled, and so untested on the client
        side. The Pause below is what the other members actually receive.
        """
        self.set_buffering(session_id, True)
        if prev == PLAYING:
            self.resume_playing = True
            # Credit the group with the time it really did play, exactly as
            # the pause path does.
            now = datetime.datetime.utcnow()
            elapsed = (now - self.last_activity).total_seconds()
            self.last_activity = now
            self.position_ticks += max(int(elapsed * TICKS_PER_SECOND), 0)
            # "Send pause command to all non-buffering sessions" -- AllReady
            # is every member that is not itself buffering, and the line above
            # has already excluded this one.
            self.send_command(session_id, "AllReady", "Pause")
        elif prev == PAUSED:
            self.resume_playing = False
            self.send_command(session_id, "CurrentSession", "Pause")
        elif not self.resume_playing:
            # Already Waiting, and for a group that was paused: force this
            # session, which should be paused, back into line.
            self.send_command(session_id, "CurrentSession", "Pause")
        self._state_update(session_id, "Buffer")

    def _waiting_stop(self, session_id, prev, **kw):
        self.state = IDLE
        self._idle_default(session_id, "Stop", WAITING)

    def _waiting_unpause(self, session_id, prev, **kw):
        # Force-override: an explicit Unpause abandons the wait.
        self.set_all_buffering(False)
        self.state = PLAYING
        self.last_activity = datetime.datetime.utcnow()
        self.send_command(session_id, "AllGroup", "Unpause")
        self._state_update(session_id, "Unpause")

    def _waiting_pause(self, session_id, prev, **kw):
        self.state = PAUSED
        self.send_command(session_id, "AllGroup", "Pause")
        self._state_update(session_id, "Pause")

    def _waiting_ready(self, session_id, prev, position_ticks=None,
                       is_playing=False, playlist_item_id=None, **kw):
        if playlist_item_id is not None and playlist_item_id != self.playing_item_id:
            # "Session reported wrong playlist item" -- resend the queue and
            # keep waiting.
            self._play_queue_update(session_id, "CurrentSession", "SetCurrentItem")
            self.set_buffering(session_id, True)
            return

        reported = self.position_ticks if position_ticks is None else position_ticks
        delay = self.position_ticks - reported

        if self.resume_playing:
            if not is_playing and abs(delay) > MAX_PLAYBACK_OFFSET_TICKS:
                # "Session got lost in time, correcting." Note it stays
                # buffering -- the client owes another Ready after this Seek.
                self.set_buffering(session_id, True)
                self.send_command(session_id, "CurrentSession", "Seek")
                self._state_update(session_id, "Ready")
                return
            self.set_buffering(session_id, False)
            if self.is_buffering():
                self.send_command(session_id, "CurrentSession", "Pause")
            else:
                self.state = PLAYING
                self.last_activity = datetime.datetime.utcnow()
                self.send_command(session_id, "AllGroup", "Unpause")
                self._state_update(session_id, "Ready")
        else:
            self.set_buffering(session_id, False)
            if not self.is_buffering():
                self.state = PAUSED
                self.send_command(session_id, "AllGroup", "Pause")
                self._state_update(session_id, "Ready")


# --------------------------------------------------------------------------
# Client side: bind a real SyncPlayManager to a group.
# --------------------------------------------------------------------------

class FakeTimesync:
    """Zero offset, so server time and local time are the same clock.

    ``skew_seconds`` back-dates every server time into the local past, which
    is what makes a scheduled play/pause run inline instead of arming a
    thread. Ten seconds is plenty for that and harmless when the only thing
    under test is what the client *sends*.

    It is not harmless when the test compares the client's position against
    the group's: ``schedule_play``'s "playing now" branch adds however long
    ago the command was supposed to happen, so a ten-second skew puts every
    client ten seconds past the group. Pass 0 there -- a ``When`` stamped a
    moment ago is already in the past, so callbacks still run inline.
    """

    def __init__(self, skew_seconds=10):
        self.subscribers = []
        self.skew = datetime.timedelta(seconds=skew_seconds)

    def server_date_to_local(self, when):
        return when - self.skew

    def local_date_to_server(self, when):
        return when

    def subscribe_time_offset(self, cb):
        self.subscribers.append(cb)

    def remove_subscriber(self, cb):
        if cb in self.subscribers:
            self.subscribers.remove(cb)

    def stop_ping(self):
        pass

    def force_update(self):
        for cb in list(self.subscribers):
            cb(datetime.timedelta(0), datetime.timedelta(milliseconds=10))


class FakeJellyfinApi:
    """The SyncPlay half of ``client.jellyfin``, wired to a group.

    Every call the client can make is here, so a method it *never* calls is
    visible as a zero in :attr:`calls` -- which is how the missing Ready shows
    up.
    """

    def __init__(self, group, session_id, deliver):
        self.group = group
        self.session_id = session_id
        self._deliver = deliver
        self.calls = []

    def _req(self, kind, **kw):
        self.calls.append((kind, kw))
        emitted = self.group.request(kind, self.session_id, **kw)
        self._deliver(emitted)

    # -- transport
    def pause_sync_play(self):
        self._req("Pause")

    def unpause_sync_play(self):
        self._req("Unpause")

    def stop_sync_play(self):
        self._req("Stop")

    def seek_sync_play(self, position_ticks):
        self._req("Seek", position_ticks=position_ticks)

    # -- the wait handshake
    def buffering_sync_play(self, when, position_ticks, is_playing, item_id):
        self._req("Buffer", position_ticks=position_ticks, is_playing=is_playing,
                  playlist_item_id=item_id)

    def ready_sync_play(self, when, position_ticks, is_playing, item_id):
        self._req("Ready", position_ticks=position_ticks, is_playing=is_playing,
                  playlist_item_id=item_id)

    # -- queue
    def next_sync_play(self, playlist_item_id):
        self._req("NextItem", playlist_item_id=playlist_item_id)

    def prev_sync_play(self, playlist_item_id):
        self._req("PreviousItem", playlist_item_id=playlist_item_id)

    def set_item_sync_play(self, playlist_item_id):
        self._req("SetPlaylistItem", playlist_item_id=playlist_item_id)

    def ignore_sync_play(self, should_ignore):
        self._req("IgnoreWait", ignore_wait=should_ignore)

    # -- group lifecycle
    def join_sync_play(self, group_id):
        self.calls.append(("Join", {"group_id": group_id}))
        self._deliver(self.group.session_joined(self.session_id))

    def leave_sync_play(self):
        self.calls.append(("Leave", {}))
        self._deliver(self.group.session_leaving(self.session_id))

    def get_sync_play(self):
        return [{"GroupId": self.group.group_id, "GroupName": "test"}]

    def ping_sync_play(self, ping):
        # `PingRequestDto.Ping` is a `long` (Jellyfin.Api/Models/SyncPlayDtos/
        # PingRequestDto.cs), so the model binder refuses a fractional value
        # and the endpoint answers 400 before any group state is touched.
        # Modelled, not waved through: a fake that accepts a float makes the
        # one call the real server was rejecting look fine.
        if isinstance(ping, bool) or not isinstance(ping, int):
            raise ValueError(
                "400 Bad Request: PingRequestDto.Ping is a long, got %r"
                % (ping,))
        self.calls.append(("Ping", {"ping": ping}))

    def new_sync_play_v2(self, *a, **kw):
        self.calls.append(("New", {"args": a}))

    def kinds(self):
        return [k for k, _ in self.calls]


class FakeClient:
    def __init__(self, api, timesync):
        self.jellyfin = api
        self.timesync = timesync


def bind(manager, group, session_id="session-under-test"):
    """Wire a real ``SyncPlayManager`` to ``group`` and enable it.

    Returns the :class:`FakeJellyfinApi`, whose ``calls`` is the record of
    everything the client sent.
    """
    timesync = FakeTimesync()

    def deliver(entries):
        for kind, targets, payload in entries:
            if session_id not in targets:
                continue
            if kind == "command":
                manager.process_command(dict(payload))
            else:
                # One dict, as the websocket delivers it (event_handler passes
                # the whole GroupUpdate through). Splitting it into two
                # positional args raised TypeError on every group update a
                # client's own request produced -- which is every Pause and
                # every Seek, since those broadcast a StateUpdate back to the
                # sender. No test had driven a request that far, so the crash
                # sat in the harness rather than in anything it measured.
                manager.process_group_update(dict(payload))

    api = FakeJellyfinApi(group, session_id, deliver)
    manager.client = FakeClient(api, timesync)
    manager.timesync = timesync
    manager.enable_sync_play(from_server=True)
    timesync.force_update()          # flips `ready` and drains the queued command
    manager.current_group = group.group_id
    group.add_session(session_id)
    return api
