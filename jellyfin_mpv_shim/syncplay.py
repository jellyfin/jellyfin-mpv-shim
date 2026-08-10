import logging
import threading
import os
from datetime import datetime, timedelta

from .clients import clientManager
from .media import Media
from .i18n import _

# This is based on: https://github.com/jellyfin/jellyfin-web/blob/master/src/components/syncPlay/syncPlayManager.js

from .conf import settings
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .player import PlayerManager as PlayerManager_type

log = logging.getLogger("syncplay")
seconds_in_ticks = 10000000
info_commands = {
    "GroupDoesNotExist": _("The specified SyncPlay group does not exist."),
    "CreateGroupDenied": _("Creating SyncPlay groups is not allowed."),
    "JoinGroupDenied": _("SyncPlay group access was denied."),
    "LibraryAccessDenied": _("Access to the SyncPlay library was denied."),
}


def _parse_precise_time(time: str):
    # We have to remove the Z and the least significant digit.
    return datetime.strptime(time[:-2], "%Y-%m-%dT%H:%M:%S.%f")


def default_group_name(client):
    """The name a group we create gets, matching jellyfin-web's
    ``SyncPlayGroupDefaultTitle``.

    One function because there are three "New Group" buttons (the OSD menu,
    the playback HUD and the browser's SyncPlay dialog) and they must not
    drift -- the browser's used to call the pre-10.7 ``new_sync_play()``,
    which posts no body at all and which every current server answers with
    a 400."""
    return _("{0}'s Group").format(clientManager.get_username_from_client(client))


class TimeoutThread(threading.Thread):
    def __init__(self, action, delay: float, args):
        self.action = action
        self.delay = delay
        self.args = args
        self.halt = threading.Event()
        threading.Thread.__init__(self)
        self.daemon = True

    def run(self):
        if not self.halt.wait(timeout=self.delay / 1000):
            try:
                self.action(*self.args)
            except:
                log.error("TimeoutThread crashed.", exc_info=True)

    def stop(self):
        # Cancel without joining: callers may hold the player lock, and the
        # callback (local_play/local_pause) blocks on that same lock — joining
        # here would deadlock. A callback that already started is expected to
        # guard itself (is_enabled / generation check) instead.
        self.halt.set()


def set_timeout(ms: float, callback, *args):
    """Similar to setTimeout JS function."""
    timeout = TimeoutThread(callback, ms, args)
    timeout.start()
    return timeout.stop


class SyncPlayManager:
    def __init__(self, manager: "PlayerManager_type"):
        self.playerManager = manager
        self.menu = manager.menu

        self.sync_enabled = False
        self.playback_diff_ms = 0
        self.method = None
        self.attempts = 0
        self.last_sync_time = datetime.utcnow()
        self.enable_speed_sync = True

        self.last_playback_waiting = None
        self.min_buffer_thresh_ms = 1000

        self.playback_rate = 1
        self.enabled_at = None
        self.ready = False
        self.is_buffering = False

        self.last_command = None
        self.queued_command = None

        self.scheduled_command = None
        self.sync_timeout = None
        self.speed_timeout = None

        # Incremented on every enable/disable (and thus group join, which
        # re-enables). Callbacks scheduled by one session capture the value and
        # bail out if it has changed by the time they fire.
        self.sync_generation = 0

        self.time_offset = timedelta(0)
        self.round_trip_duration = timedelta(0)
        self.notify_sync_ready = False

        self.read_callback = None
        self.timesync = None
        self.client = None
        self.current_group = None
        self.playqueue_last_updated = None

        # Whether we are playing the group's content, as opposed to merely
        # being a member of it -- jellyfin-web's Manager.followingGroupPlayback.
        # Stopping playback halts (SyncPlay/SetIgnoreWait), it does not leave:
        # membership is a thing you exit from the SyncPlay menu, and tearing
        # it down on stop meant every "back to the library" dropped the group
        # out from under the rest of its members. See
        # PlayerManager._release_syncplay for the one case that still leaves.
        self.following = True
        # The last PlayQueue update the group sent. Kept so a halted session
        # has something to resume into (web's QueueCore.startPlayback reads
        # the same stored queue).
        self.last_playqueue = None

    # On playback time update (call from timeline push)
    def sync_playback_time(self):
        if (
            not self.last_command
            or self.last_command["Command"] != "Unpause"
            or self.is_buffering
            or not self.playerManager.is_not_paused()
            or self.playerManager.menu.is_menu_shown
        ):
            log.debug("Not syncing due to no playback.")
            return

        current_time = datetime.utcnow()

        # Avoid overloading player
        elapsed = current_time - self.last_sync_time
        if elapsed.total_seconds() * 1000 < settings.sync_method_thresh / 2:
            log.debug("Not syncing due to threshold.")
            return

        self.last_sync_time = current_time
        play_at_time = self.last_command["When"]

        current_position_ticks = int(self.playerManager.get_time() * seconds_in_ticks)
        server_position_ticks = (
            self.last_command["PositionTicks"]
            + ((current_time - play_at_time) + self.time_offset).total_seconds()
            * seconds_in_ticks
        )

        diff_ms = (server_position_ticks - current_position_ticks) / 10000
        self.playback_diff_ms = diff_ms

        if self.sync_enabled:
            abs_diff_ms = abs(diff_ms)

            if (
                self.enable_speed_sync
                and settings.sync_max_delay_speed
                < abs_diff_ms
                < settings.sync_method_thresh
            ):
                if self.attempts > settings.sync_speed_attempts:
                    self.enable_speed_sync = False
                    return

                # Speed To Sync Method
                speed = 1 + diff_ms / settings.sync_speed_time

                self.playerManager.set_speed(speed)
                self.sync_enabled = False
                self.attempts += 1
                log.info("SyncPlay Speed to Sync rate: {0}".format(speed))
                self.player_message(_("SpeedToSync (x{0})").format(speed))

                generation = self.sync_generation

                def callback():
                    # Only restore speed if SyncPlay is still enabled and the
                    # command wasn't superseded (disable/leave/rejoin/clear)
                    # since this restore was scheduled.
                    if not self._still_current(generation):
                        return
                    self.playerManager.set_speed(1)
                    self.sync_enabled = True

                self.speed_timeout = set_timeout(settings.sync_speed_time, callback)
            elif abs_diff_ms > settings.sync_max_delay_skip:
                if self.attempts > settings.sync_attempts:
                    self.sync_enabled = False
                    log.info("SyncPlay Sync Disabled due to too many attempts.")
                    self.player_message(_("Sync Disabled (Too Many Attempts)"))
                    return

                # Skip To Sync Method
                self.local_seek(server_position_ticks / seconds_in_ticks)
                self.sync_enabled = False
                self.attempts += 1
                log.info("SyncPlay Skip to Sync Activated")
                self.player_message(_("SkipToSync (x{0})").format(self.attempts))

                set_timeout(settings.sync_method_thresh / 2,
                            self._rearm_sync, self.sync_generation)
            else:
                if self.attempts > 0:
                    log.info(
                        "Playback synced after {0} attempts.".format(self.attempts)
                    )
                self.attempts = 0

    # On timesync update
    def on_timesync_update(self, time_offset: timedelta, ping: timedelta):
        self.time_offset = time_offset
        self.round_trip_duration = ping * 2

        if self.notify_sync_ready:
            self.ready = True
            self.notify_sync_ready = False
            if self.read_callback:
                self.read_callback()
                self.read_callback = None

        if self.sync_enabled:
            try:
                # PingRequestDto declares `long Ping`, so a fractional value
                # fails to bind and the whole request comes back 400 -- which
                # is what the "Server responds with 400 bad request" note that
                # used to sit here was recording. Every ping this client ever
                # sent was rejected, so the server fell back to its default
                # for us and compensated the group's unpause delay with the
                # wrong latency.
                self.client.jellyfin.ping_sync_play(
                    round(ping.total_seconds() * 1000))
            except Exception:
                log.error("Syncplay ping reporting failed.")

    def enable_sync_play(self, from_server: bool):
        self.sync_generation += 1
        # A fresh join always follows: a halt belongs to the group it was
        # made in, and joining is the user asking to watch along.
        self.following = True
        self.last_playqueue = None
        self.playback_rate = self.playerManager.get_speed()
        self.enabled_at = datetime.utcnow()
        self.enable_speed_sync = True
        # #16: take every key that currently means pause or seek, for as
        # long as the group lasts.
        #
        # There are two paths into SyncPlay and they are not equivalent. The
        # direct one -- a key -> toggle_pause -> pause_request -- never lets
        # mpv unpause in a group at all. The defensive observer on `pause`
        # catches what we did not initiate, but only *after* mpv has already
        # acted, so it plays and then forces a re-pause while the group is
        # asked: a visible flicker on every play. [iw]: "it's not ideal,
        # worth capturing when using syncplay."
        #
        # So the direct path is kept, and scoped to when it is needed rather
        # than held for the life of the process. It also reaches further
        # than the fixed bindings ever did -- `p`, PLAYPAUSE, REWIND,
        # Shift+arrows and anything the user remapped were all going through
        # the observer before.
        self._claim_keys(True)

        def ready_callback():
            self.process_command(self.queued_command)
            self.queued_command = None
            self.enabled_at = self.timesync.local_date_to_server(self.enabled_at)

        self.read_callback = ready_callback

        self.ready = False
        self.notify_sync_ready = True

        if not from_server:
            self.client = self.playerManager.get_current_client()

        timesync = self.client.timesync
        if self.timesync is not None and timesync is not self.timesync:
            self.timesync.remove_subscriber(self.on_timesync_update)
            self.timesync.stop_ping()

        self.timesync = timesync
        self.timesync.subscribe_time_offset(self.on_timesync_update)
        self.timesync.force_update()

        log.info("Syncplay enabled.")
        if from_server:
            self.player_message(_("SyncPlay enabled."))

    def disable_sync_play(self, from_server: bool):
        # Mark the session disabled *before* tearing down the scheduled
        # commands, so that any callback still in flight (a TimeoutThread we are
        # about to join) sees is_enabled() == False and no-ops instead of
        # yanking the player around after the group was left.
        self.enabled_at = None
        self.sync_generation += 1
        # ...and give the keys back. Outside a group they are mpv's, and
        # the user's own bindings apply untouched.
        self._claim_keys(False)

        # Cancel every pending scheduled command / sync timer / speed restore.
        # clear_scheduled_command() also resets the player speed to 1.
        self.clear_scheduled_command()
        self.playerManager.set_speed(self.playback_rate)

        self.ready = False
        self.last_command = None
        self.queued_command = None
        self.sync_enabled = False
        self.playqueue_last_updated = None
        self.following = True
        self.last_playqueue = None

        # Per-session drift and buffer bookkeeping. None of it can clear
        # itself once the session is over: on_buffer_done is only ever reached
        # behind is_enabled(), and `attempts` is reset in exactly one place —
        # the in-sync branch of sync_playback_time, which a session that gave
        # up (sync_enabled False) can no longer reach at all.
        #
        # So a stall that outlived a session latched is_buffering, and the
        # *next* group got no drift correction ("Not syncing due to no
        # playback", forever) and never reported a stall to anyone, while a
        # non-None last_playback_waiting made on_buffer a no-op. Carried-over
        # attempts spent the next group's budget before it began: enable
        # restores enable_speed_sync deliberately to give a session a fresh
        # start, and that restore was defeated by the counter it depends on —
        # the first correction hit the cap and reported "Sync Disabled (Too
        # Many Attempts)" in a group this client had attempted nothing in.
        #
        # halt_group_playback calls itself "the same teardown as disable,
        # minus the membership" and clears the buffer pair; this is the rest
        # of that symmetry.
        self.is_buffering = False
        self.last_playback_waiting = None
        self.attempts = 0
        self.enable_speed_sync = True
        self.playback_diff_ms = 0
        self.method = None

        if self.timesync is not None:
            self.timesync.remove_subscriber(self.on_timesync_update)
            self.timesync.stop_ping()
            self.timesync = None
        self.current_group = None

        log.info("Syncplay disabled.")
        if from_server:
            self.player_message(_("SyncPlay disabled."))
        else:
            try:
                self.client.jellyfin.leave_sync_play()
            except:
                log.warning("Failed to leave syncplay.", exc_info=True)

    def halt_group_playback(self):
        """Stop playing the group's content without leaving the group.

        jellyfin-web's ``Manager.haltGroupPlayback``: ``SetIgnoreWait`` tells
        the server to stop holding the group up for a member who is no longer
        watching, and membership survives. This is what stopping playback does
        now -- the alternative, leaving, meant the only way to pause your own
        evening was to drop out of everyone else's.

        The command stream keeps being *recorded* while halted (see
        ``process_command``); it just stops being applied. That record is what
        lets ``resume_group_playback`` land at the group's current position
        rather than wherever the queue update left it."""
        if not self.is_enabled():
            return
        self.following = False
        # Same teardown as disable, minus the membership: nothing armed by the
        # session we are stepping out of may fire at a player that is stopping.
        self.clear_scheduled_command()
        self.playerManager.set_speed(self.playback_rate)
        self.is_buffering = False
        self.last_playback_waiting = None
        log.info("Syncplay playback halted (still in the group).")
        self._set_ignore_wait(True)

    def follow_group_playback(self):
        """Start following the group's playback again (web:
        ``followGroupPlayback``). Does not itself start anything playing --
        ``resume_group_playback`` is that, and a ``NewPlaylist`` queue update
        is the other way back in."""
        if not self.is_halted():
            return
        self.following = True
        log.info("Syncplay is following group playback again.")
        self._set_ignore_wait(False)

    def _set_ignore_wait(self, ignore: bool):
        try:
            self.client.jellyfin.ignore_sync_play(ignore)
        except Exception:
            # Advisory: it only decides whether the group waits for us. Losing
            # it must not leave `following` disagreeing with what we are doing.
            log.warning("Failed to set the SyncPlay ignore-wait flag.",
                        exc_info=True)

    def can_resume(self):
        """Whether ``resume_group_playback`` has something to play."""
        return (self.is_halted()
                and bool((self.last_playqueue or {}).get("Playlist")))

    def resume_group_playback(self):
        """Rejoin the group's playback after a halt (web:
        ``resumeGroupPlayback``)."""
        if not self.in_group():
            return
        self.follow_group_playback()
        data = self.last_playqueue
        if not data or not data.get("Playlist"):
            log.info("Syncplay resume: the group has nothing queued.")
            return
        self._start_queue(data, self._resume_position(data))

    def _resume_position(self, data):
        """Where the group is *now*, in seconds, or None.

        The stored queue's StartPositionTicks is where the group was when the
        queue last changed, which for anything but a just-started playlist is
        minutes stale. web's ``estimateCurrentTicks`` prefers the last playback
        command for the same reason; unlike web we only run the clock forward
        for an Unpause, since advancing from a Pause is just wrong."""
        cmd = self.last_command
        try:
            if cmd is not None and self._command_is_newer(cmd, data):
                ticks = cmd["PositionTicks"]
                if cmd["Command"] == "Unpause":
                    now = self.timesync.local_date_to_server(datetime.utcnow())
                    ticks += (now - cmd["When"]).total_seconds() * seconds_in_ticks
                return max(0.0, ticks / seconds_in_ticks)
        except Exception:
            log.debug("Could not estimate the resume position.", exc_info=True)
        offset = data.get("StartPositionTicks")
        return None if offset is None else offset / seconds_in_ticks

    @staticmethod
    def _command_is_newer(cmd, data):
        last_update = data.get("LastUpdate")
        if last_update is None:
            return True
        return cmd["EmitttedAt"] >= _parse_precise_time(last_update)

    def _buffer_req(self, is_buffering):
        if self.timesync is None:
            # This can get called before it is ready...
            return
        video = self.playerManager.get_video()
        if video is None:
            # Nothing playing: there is no position or playlist item to
            # report. Reached from the Seek path, which can land after a stop.
            return
        media = video.parent
        when = self.timesync.local_date_to_server(datetime.utcnow())
        ticks = int(self.playerManager.get_time() * seconds_in_ticks)
        playing = self.playerManager.is_not_paused()
        playlist_id = media.queue[media.seq]["PlaylistItemId"]

        if is_buffering:
            self.client.jellyfin.buffering_sync_play(when, ticks, playing, playlist_id)
        else:
            self.client.jellyfin.ready_sync_play(when, ticks, playing, playlist_id)

    # On Buffer
    def on_buffer(self):
        if not self.last_playback_waiting:
            playback_waiting = datetime.utcnow()
            self.last_playback_waiting = playback_waiting
            generation = self.sync_generation

            def handle_buffer():
                # The one scheduled callback here that had no generation
                # check: its own guard is the timestamp, which a *leave* used
                # not to clear (halt did), so a debounce armed a second before
                # leaving fired afterwards and set is_buffering on a manager
                # in no group — which then suppressed drift correction for
                # whatever session came next.
                if not self._still_current(generation):
                    return
                if playback_waiting == self.last_playback_waiting:
                    self._buffer_req(True)
                    self.is_buffering = True

            set_timeout(self.min_buffer_thresh_ms, handle_buffer)

    # On Buffer Done
    def on_buffer_done(self):
        if self.is_buffering:
            self._buffer_req(False)

        self.last_playback_waiting = None
        self.is_buffering = False

    def play_done(self):
        self.local_pause()
        self._buffer_req(False)

    # Three states, and which name gets which is deliberate.
    #
    # Halting (stopping playback without leaving the group) split what used to
    # be one question in two: "are we a member" and "are we playing the
    # group's content". Almost every caller wants the second -- forwarding a
    # pause, answering a buffer, suppressing skip-intro -- and a caller that
    # asks the wrong one broadcasts a user's private pause to five friends.
    #
    # So ``is_enabled`` keeps its original meaning rather than being widened
    # to cover membership. The habitual spelling stays the safe one, the new
    # state has to be asked for by name, and the cost of forgetting
    # ``in_group`` somewhere is a group missing from a dialog.

    def in_group(self):
        """A member of a group, whether or not we are watching along.

        Rarely what you want: it is true of a halted session, which is one
        that has stopped playing. Membership questions only -- leaving,
        listing, reporting which group we are in."""
        return self.enabled_at is not None

    def _claim_keys(self, wanted):
        """Claim or release pause+seek on the player. Best effort: a player
        that cannot do this (an old mpv, a stand-in in a test) must not stop
        a group being joined or left."""
        try:
            from .keysweep import PAUSE, SEEK

            claim = getattr(self.playerManager, "claim_keys", None)
            if claim is not None:
                claim("syncplay", {PAUSE, SEEK} if wanted else None)
            # ...and tell the renderer, whose own pause paths (click-to-pause,
            # the summon key, right-click in mpv modality) hand over to
            # Python only while a group is on. Without this a group joined
            # mid-playback keeps the local `cycle pause`, which is not a
            # pause at all in a group.
            notify = getattr(self.playerManager, "on_syncplay_change", None)
            if notify is not None:
                notify()
        except Exception:
            log.debug("could not update the SyncPlay key claim",
                      exc_info=True)

    def is_enabled(self):
        """SyncPlay is driving, and being driven by, this player.

        The default question, and the one to reach for when unsure."""
        return self.in_group() and self.following

    def is_halted(self):
        """In a group, having stopped playing its content (web's
        ``!isFollowingGroupPlayback``). Pairs with ``halt_group_playback``;
        ``resume_group_playback`` is the way out of it."""
        return self.in_group() and not self.following

    def process_group_update(self, command: dict):
        command_type = command["Type"]
        log.debug("Syncplay group update: {0}".format(command))
        if command_type in info_commands:
            self.player_message(info_commands[command_type])
        elif command_type == "PrepareSession":
            self.prepare_session(command["GroupId"], command["Data"])
        elif command_type == "GroupJoined":
            self.current_group = command["GroupId"]
            self.enable_sync_play(True)
        elif command_type == "GroupLeft" or command_type == "NotInGroup":
            self.disable_sync_play(True)
        elif command_type == "UserJoined":
            self.player_message(_("{0} has joined.").format(command["Data"]))
        elif command_type == "UserLeft":
            self.player_message(_("{0} has left.").format(command["Data"]))
        elif command_type == "GroupWait":
            self.player_message(_("{0} is buffering.").format(command["Data"]))
        elif command_type == "PlayQueue":
            self.upd_queue(command["Data"])
        elif command_type == "StateUpdate":
            log.info(
                "{0} state caused by {1}".format(
                    command["Data"]["State"], command["Data"]["Reason"]
                )
            )
        else:
            log.error(
                "Unknown SyncPlay command {0} payload {1}.".format(
                    command_type, command
                )
            )

    def process_command(self, command: Optional[dict]):
        if command is None:
            return

        if not self.in_group():
            log.debug(
                "Ignoring command {0} due to SyncPlay being disabled.".format(command)
            )
            return

        if not self.ready:
            log.debug(
                "Queued command {0} due to SyncPlay not being ready.".format(command)
            )
            self.queued_command = command
            return

        command["When"] = _parse_precise_time(command["When"])
        command["EmitttedAt"] = _parse_precise_time(command["EmittedAt"])

        if command["EmitttedAt"] < self.enabled_at:
            log.debug("Ignoring old command {0}.".format(command))
            return

        if (
            self.last_command
            and self.last_command["When"] == command["When"]
            and self.last_command["PositionTicks"] == command["PositionTicks"]
            and self.last_command["Command"] == command["Command"]
            # EmittedAt too, or this drops the server's only way of correcting
            # a client it thinks has drifted. Group.NewSyncPlayCommand stamps
            # every command with DateTime.UtcNow, so a resync differs from the
            # broadcast it repeats in exactly this field and no other -- and
            # without it the resync looked like a duplicate and was discarded.
            # What remains filtered is a literal re-delivery of one message.
            and self.last_command["EmitttedAt"] == command["EmitttedAt"]
        ):
            log.debug("Ignoring duplicate command {0}.".format(command))
            return

        self.last_command = command
        command_cmd, when, position = (
            command["Command"],
            command["When"],
            command["PositionTicks"],
        )
        log.info(
            "Syncplay will {0} at {1} position {2}".format(command_cmd, when, position)
        )

        if not self.following:
            # Recorded above, applied never. web gets the same effect by
            # letting PlaybackCore schedule it and having every local_* call
            # no-op with no active player; stopping here is the same answer
            # without arming a timer that would seek an idle player. The
            # record is what _resume_position reads.
            log.debug("Not applying command while halted.")
            return

        if command_cmd == "Unpause":
            self.schedule_play(when, position)
        elif command_cmd == "Pause":
            self.schedule_pause(when, position)
        elif command_cmd == "Seek":
            self.schedule_seek(when, position)
        elif command_cmd == "Stop":
            self.schedule_stop()
        else:
            log.error("Command {0} is unknown.".format(command_cmd))

    def prepare_session(self, group_id: str, session_data: dict):
        # Dead, and now checked rather than suspected: `GroupUpdateType`
        # (MediaBrowser.Model/SyncPlay) is UserJoined, UserLeft, GroupJoined,
        # GroupLeft, StateUpdate, PlayQueue, NotInGroup, GroupDoesNotExist,
        # LibraryAccessDenied -- there is no PrepareSession for a server to
        # send. Kept for older servers; not removed blind.
        #
        # It could not work as written anyway: the payload carries ItemIds and
        # no playlist ids, so the Media below invents them, and every SyncPlay
        # report of an invented id is a 400 (the server's DTOs declare that
        # field a Guid). Anything reviving this path has to solve that first.
        play_command = session_data.get("PlayCommand")
        if not self.playerManager.has_video():
            play_command = "PlayNow"

        seq = session_data.get("StartIndex")
        if seq is None:
            seq = 0
        media = Media(
            self.client,
            session_data.get("ItemIds"),
            seq=seq,
            user_id=session_data.get("ControllingUserId"),
            aid=session_data.get("AudioStreamIndex"),
            sid=session_data.get("SubtitleStreamIndex"),
            srcid=session_data.get("MediaSourceId"),
        )

        if (
            self.playerManager.has_video()
            and self.playerManager.get_video().item_id == session_data["ItemIds"][0]
            and play_command == "PlayNow"
        ):
            # We assume the video is already available.
            self.playerManager.get_video().parent = media
            log.info("Syncplay Session Prepare: {0} {1}".format(group_id, session_data))
            self.local_seek(
                (session_data.get("PositionTicks", 0) or 0) / seconds_in_ticks
            )
            self.current_group = group_id
        elif play_command == "PlayNow":
            log.info(
                "Syncplay Session Recreate: {0} {1}".format(group_id, session_data)
            )
            offset = session_data.get("StartPositionTicks")
            if offset is not None:
                offset /= 10000000

            video = media.video
            if video:
                if settings.pre_media_cmd:
                    os.system(settings.pre_media_cmd)
                self.playerManager.play(
                    video, offset, no_initial_timeline=True, is_initial_play=True
                )
                self.playerManager.send_timeline()
                # Really not sure why I have to call this.
                self.join_group(group_id)
                self.playerManager.timeline_handle()
                if settings.play_cmd:
                    os.system(settings.play_cmd)
        elif play_command == "PlayLast":
            self.playerManager.get_video().parent.insert_items(
                session_data.get("ItemIds"), append=True
            )
            self.playerManager.upd_player_hide()
        elif play_command == "PlayNext":
            self.playerManager.get_video().parent.insert_items(
                session_data.get("ItemIds"), append=False
            )
            self.playerManager.upd_player_hide()

    def player_message(self, message: str):
        # Messages overwrite menu, so they are ignored.
        if self.menu.is_menu_shown:
            log.info("Ignored SyncPlay Message (menu): {0}".format(message))
            return

        notify = getattr(self.playerManager, "notify_syncplay", None)
        if notify is not None:
            # The in-window UI has a surface of its own and mpv's OSD text
            # draws *under* the mpvtk overlay bitmaps, so on the new UI these
            # were a message either painted over or drawn on top of the
            # browser's own status line. Same split as notify_update.
            log.info("SyncPlay Message: {0}".format(message))
            try:
                notify(message)
            except Exception:
                log.debug("SyncPlay notify failed.", exc_info=True)
            return

        if settings.sync_osd_message:
            self.playerManager.show_text(message, 2000)
        else:
            log.info("SyncPlay Message: {0}".format(message))

    def schedule_play(self, when: datetime, position: int):
        self.clear_scheduled_command()
        current_time = datetime.utcnow()
        local_play_time = self.timesync.server_date_to_local(when)

        if local_play_time > current_time:
            log.debug("SyncPlay Scheduled Play: Playing Later")
            play_timeout = (local_play_time - current_time).total_seconds() * 1000
            self.local_seek(position / seconds_in_ticks)

            generation = self.sync_generation

            def scheduled():
                if not self._still_current(generation):
                    return
                self.local_play()
                self.sync_timeout = set_timeout(
                    settings.sync_method_thresh / 2, self._rearm_sync, generation
                )

            self.scheduled_command = set_timeout(play_timeout, scheduled)
        else:
            log.debug("SyncPlay Scheduled Play: Playing Now")
            # Group playback already started
            server_position_secs = (
                position / seconds_in_ticks
                + (current_time - local_play_time).total_seconds()
            )
            self.local_play()
            self.local_seek(server_position_secs)

            self.sync_timeout = set_timeout(
                settings.sync_method_thresh / 2, self._rearm_sync,
                self.sync_generation
            )

    def schedule_pause(self, when: datetime, position: int, seek_only: bool = False,
                       report_ready: bool = False):
        self.clear_scheduled_command()
        current_time = datetime.utcnow()
        local_pause_time = self.timesync.server_date_to_local(when)

        generation = self.sync_generation

        def callback():
            if not self._still_current(generation):
                return
            if not seek_only:
                self.local_pause()
            self.local_seek(position / seconds_in_ticks)
            if report_ready:
                # A Seek command means the group moved to Waiting and set
                # every member buffering; it resumes only once all of them
                # answer Ready. We never did, so a group seek left everyone
                # stopped until somebody pressed play (which force-overrides
                # the wait). It looked intermittent because a seek slower than
                # min_buffer_thresh_ms went out as Buffering and came back as
                # Ready by that route -- so it hung on fast local files and
                # worked on slow streams.
                self._buffer_req(False)

        if local_pause_time > current_time:
            log.debug("SyncPlay Scheduled Pause/Seek: Pausing Later")
            pause_timeout = (local_pause_time - current_time).total_seconds() * 1000
            self.scheduled_command = set_timeout(pause_timeout, callback)
        else:
            log.debug("SyncPlay Scheduled Pause/Seek: Pausing Now")
            callback()

    def _play_video(self, video, offset):
        if video:
            if settings.pre_media_cmd:
                os.system(settings.pre_media_cmd)
            self.playerManager.play(video, offset, no_initial_timeline=True)
            if settings.play_cmd:
                os.system(settings.play_cmd)
        else:
            log.error("No video from queue update.")

    def _start_queue(self, data, offset, sp_items=None):
        """Start playing a group play queue from scratch."""
        if sp_items is None:
            sp_items = [
                {"Id": x["ItemId"], "PlaylistItemId": x["PlaylistItemId"]}
                for x in data["Playlist"]
            ]
        media = Media(
            self.client, sp_items, data["PlayingItemIndex"], queue_override=False
        )
        self._play_video(media.video, offset)

    def upd_queue(self, data):
        # It can't hurt to update the queue lol.
        # last_upd = _parse_precise_time(data["LastUpdate"])
        # if (
        #    self.playqueue_last_updated is not None
        #    and self.playqueue_last_updated >= last_upd
        # ):
        #    log.warning("Tried to apply old queue update.")
        #    return
        #
        # self.playqueue_last_updated = last_upd

        self.last_playqueue = data
        if not self.following:
            # Halted. A brand-new playlist pulls us back in -- the group put
            # something on and that is an invitation -- but its progress
            # through content we are not watching is not. This is web's
            # QueueCore: NewPlaylist calls followGroupPlayback, and every
            # other reason returns early on isFollowingGroupPlayback.
            if data.get("Reason") != "NewPlaylist":
                log.debug("Ignoring %s queue update while halted.",
                          data.get("Reason"))
                return
            self.follow_group_playback()

        sp_items = [
            {"Id": x["ItemId"], "PlaylistItemId": x["PlaylistItemId"]}
            for x in data["Playlist"]
        ]
        if self.playerManager.get_video() is None:
            log.info("The queue update changed the video. (New)")
            offset = data.get("StartPositionTicks")
            if offset is not None:
                offset /= 10000000

            self._start_queue(data, offset, sp_items=sp_items)
        else:
            media = self.playerManager.get_video().parent
            new_media = media.replace_queue(sp_items, data["PlayingItemIndex"])
            if new_media:
                log.info("The queue update changed the video.")
                offset = data.get("StartPositionTicks")
                if offset is not None:
                    offset /= 10000000

                self._play_video(new_media.video, offset)
            else:
                self._buffer_req(False)

    def schedule_seek(self, when: datetime, position: int):
        # This replicates what the web client does.
        #
        # report_ready is ours: only WaitingGroupState emits a Seek command
        # (every other state routes a seek through it first), so a Seek always
        # means the group is waiting on us. Pause carries no such promise --
        # answering one in the Paused state gets "client got lost, sending
        # current state" back, and that Pause would answer itself forever.
        self.schedule_pause(when, position, report_ready=True)

    def schedule_stop(self):
        """The group stopped playback.

        Stop, but stay in the group: the server moves the group to Idle and
        keeps every session in it, so a later Play has to find us still there.
        That is why this cannot go through PlayerManager.stop() directly --
        that one disables SyncPlay on the way past.

        Queued rather than called: this runs on the websocket reader thread,
        and stop() takes the player lock and then the timeline lock. Doing
        that here would block every other message from this server behind it,
        and would add a fresh path into the lock inversion in finding 1 of
        docs/archive/SYNCPLAY_FINDINGS.md.
        """
        self.clear_scheduled_command()
        self.playerManager.put_task(self.playerManager.stop, False)

    def _still_current(self, generation):
        """Whether a callback armed under `generation` may still act. False
        once the session was disabled OR the command it belongs to was
        superseded (every clear_scheduled_command bumps the generation, and
        halting is one of those -- which is why this asks in_group rather than
        is_enabled and does not need to test both)."""
        return self.in_group() and self.sync_generation == generation

    def _rearm_sync(self, generation):
        """Deferred re-enable of drift correction after a scheduled play/skip,
        unless the session was disabled or superseded meanwhile."""
        if self._still_current(generation):
            self.sync_enabled = True

    def clear_scheduled_command(self):
        # Supersede anything armed or mid-flight: TimeoutThread.stop() only
        # cancels a timer that hasn't fired, so a callback already past its
        # halt check relies on this bump (checked via _still_current) to
        # no-op instead of applying a stale command. Stop the deferred
        # play/pause command first: it may itself (re)arm sync_timeout, and
        # clearing sync_timeout afterwards catches that.
        self.sync_generation += 1
        if self.scheduled_command is not None:
            self.scheduled_command()
            self.scheduled_command = None

        if self.sync_timeout is not None:
            self.sync_timeout()
            self.sync_timeout = None

        if self.speed_timeout is not None:
            self.speed_timeout()
            self.speed_timeout = None

        self.sync_enabled = False
        self.playerManager.set_speed(1)

    def play_request(self):
        self.client.jellyfin.unpause_sync_play()

    def pause_request(self):
        self.client.jellyfin.pause_sync_play()
        self.playerManager.set_paused(True, True)

    def seek_request(self, offset: float):
        self.client.jellyfin.seek_sync_play(int(offset * seconds_in_ticks))

    def local_play(self):
        self.playerManager.set_paused(False, True)

    def local_pause(self):
        self.playerManager.set_paused(True, True)

    def local_seek(self, offset: float):
        self.playerManager.seek(offset, absolute=True, force=True)

    def join_group(self, group_id: str):
        self.client.jellyfin.join_sync_play(group_id)

    def discord_join_group(self, join_secret):
        self.enable_sync_play(False)
        self.client.jellyfin.join_sync_play(join_secret["secret"])

    def menu_join_group(self):
        group = self.menu.menu_list[self.menu.menu_selection][2]
        self.menu.hide_menu()
        self.client.jellyfin.join_sync_play(group["GroupId"])

    def menu_disable(self):
        self.menu.hide_menu()
        self.client.jellyfin.leave_sync_play()

    def menu_create_group(self):
        self.menu.hide_menu()
        self.client.jellyfin.new_sync_play_v2(default_group_name(self.client))

    def menu_action(self):
        self.client = self.playerManager.get_current_client()

        selected = 0
        offset = 1
        group_option_list = [
            (_("None (Disabled)"), self.menu_disable, None),
        ]
        if not self.in_group():
            offset = 2
            group_option_list.append((_("New Group"), self.menu_create_group, None))
        groups = self.client.jellyfin.get_sync_play()
        for i, group in enumerate(groups):
            group_option_list.append((group["GroupName"], self.menu_join_group, group))
            if group["GroupId"] == self.current_group:
                selected = i + offset
        self.menu.put_menu(_("SyncPlay"), group_option_list, selected)

    def request_next(self, playlist_item_id):
        self.client.jellyfin.next_sync_play(playlist_item_id)

    def request_prev(self, playlist_item_id):
        self.client.jellyfin.prev_sync_play(playlist_item_id)

    def request_skip(self, playlist_item_id):
        self.client.jellyfin.set_item_sync_play(playlist_item_id)
