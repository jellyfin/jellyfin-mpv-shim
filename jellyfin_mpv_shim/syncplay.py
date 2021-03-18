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
        self.halt.set()
        self.join()


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

        self.time_offset = timedelta(0)
        self.round_trip_duration = timedelta(0)
        self.notify_sync_ready = False

        self.read_callback = None
        self.timesync = None
        self.client = None
        self.current_group = None
        self.playqueue_last_updated = None

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

                def callback():
                    self.playerManager.set_speed(1)
                    self.sync_enabled = True

                set_timeout(settings.sync_speed_time, callback)
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

                def callback():
                    self.sync_enabled = True

                set_timeout(settings.sync_method_thresh / 2, callback)
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

        # Server responds with 400 bad request...
        if self.sync_enabled:
            try:
                self.client.jellyfin.ping_sync_play(ping.total_seconds() * 1000)
            except Exception:
                log.error("Syncplay ping reporting failed.")

    def enable_sync_play(self, from_server: bool):
        self.playback_rate = self.playerManager.get_speed()
        self.enabled_at = datetime.utcnow()
        self.enable_speed_sync = True

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
        self.playerManager.set_speed(self.playback_rate)

        self.enabled_at = None
        self.ready = False
        self.last_command = None
        self.queued_command = None
        self.sync_enabled = False
        self.playqueue_last_updated = None

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

    def _buffer_req(self, is_buffering):
        if self.timesync is None:
            # This can get called before it is ready...
            return
        media = self.playerManager.get_video().parent
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

            def handle_buffer():
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

    def is_enabled(self):
        return self.enabled_at is not None

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

        if not self.is_enabled():
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

        if command_cmd == "Unpause":
            self.schedule_play(when, position)
        elif command_cmd == "Pause":
            self.schedule_pause(when, position)
        elif command_cmd == "Seek":
            self.schedule_seek(when, position)
        else:
            log.error("Command {0} is unknown.".format(command_cmd))

    def prepare_session(self, group_id: str, session_data: dict):
        # I think this might be a dead code path
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
                self.playerManager.play(video, offset, no_initial_timeline=True)
                self.playerManager.send_timeline()
                # Really not sure why I have to call this.
                self.join_group(group_id)
                self.playerManager.timeline_handle()
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
        if not self.menu.is_menu_shown:
            if settings.sync_osd_message:
                self.playerManager.show_text(message, 2000)
            else:
                log.info("SyncPlay Message: {0}".format(message))
        else:
            log.info("Ignored SyncPlay Message (menu): {0}".format(message))

    def schedule_play(self, when: datetime, position: int):
        self.clear_scheduled_command()
        current_time = datetime.utcnow()
        local_play_time = self.timesync.server_date_to_local(when)

        if local_play_time > current_time:
            log.debug("SyncPlay Scheduled Play: Playing Later")
            play_timeout = (local_play_time - current_time).total_seconds() * 1000
            self.local_seek(position / seconds_in_ticks)

            def scheduled():
                self.local_play()

                def sync_timeout():
                    self.sync_enabled = True

                self.sync_timeout = set_timeout(
                    settings.sync_method_thresh / 2, sync_timeout
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

            def sync_timeout():
                self.sync_enabled = True

            self.sync_timeout = set_timeout(
                settings.sync_method_thresh / 2, sync_timeout
            )

    def schedule_pause(self, when: datetime, position: int, seek_only: bool = False):
        self.clear_scheduled_command()
        current_time = datetime.utcnow()
        local_pause_time = self.timesync.server_date_to_local(when)

        def callback():
            if not seek_only:
                self.local_pause()
            self.local_seek(position / seconds_in_ticks)

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
        else:
            log.error("No video from queue update.")

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

        sp_items = [
            {"Id": x["ItemId"], "PlaylistItemId": x["PlaylistItemId"]}
            for x in data["Playlist"]
        ]
        if self.playerManager.get_video() is None:
            media = Media(
                self.client, sp_items, data["PlayingItemIndex"], queue_override=False
            )
            log.info("The queue update changed the video. (New)")
            offset = data.get("StartPositionTicks")
            if offset is not None:
                offset /= 10000000

            self._play_video(media.video, offset)
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
        self.schedule_pause(when, position)

    def clear_scheduled_command(self):
        if self.scheduled_command is not None:
            self.scheduled_command()

        if self.sync_timeout is not None:
            self.sync_timeout()

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

    def menu_join_group(self):
        group = self.menu.menu_list[self.menu.menu_selection][2]
        self.menu.hide_menu()
        self.client.jellyfin.join_sync_play(group["GroupId"])

    def menu_disable(self):
        self.menu.hide_menu()
        self.client.jellyfin.leave_sync_play()

    def menu_create_group(self):
        self.menu.hide_menu()
        self.client.jellyfin.new_sync_play_v2(
            _("{0}'s Group").format(clientManager.get_username_from_client(self.client))
        )

    def menu_action(self):
        self.client = self.playerManager.get_current_client()

        selected = 0
        offset = 1
        group_option_list = [
            (_("None (Disabled)"), self.menu_disable, None),
        ]
        if not self.is_enabled():
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
