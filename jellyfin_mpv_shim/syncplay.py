import logging
import threading
import os
from datetime import datetime, timedelta
from .media import Media
from time import sleep

# This is based on: https://github.com/jellyfin/jellyfin-web/blob/master/src/components/syncPlay/syncPlayManager.js

from .conf import settings

log = logging.getLogger('syncplay')
seconds_in_ticks = 10000000
info_commands = {
    "GroupDoesNotExist": "The specified SyncPlay group does not exist.",
    "CreateGroupDenied": "Creating SyncPlay groups is not allowed.",
    "JoinGroupDenied": "SyncPlay group access was denied.",
    "LibraryAccessDenied": "Access to the SyncPlay library was denied."
}


def _parse_precise_time(time):
    # We have to remove the Z and the least significant digit.
    return datetime.strptime(time[:-2], "%Y-%m-%dT%H:%M:%S.%f")


class TimeoutThread(threading.Thread):
    def __init__(self, action, delay, args):
        self.action = action
        self.delay = delay
        self.args = args
        self.halt = threading.Event()
        threading.Thread.__init__(self)
        self.daemon = True

    def run(self):
        if not self.halt.wait(timeout=self.delay/1000):
            self.action(*self.args)

    def stop(self):
        self.halt.set()
        self.join()


def set_timeout(ms: float, callback, *args):
    """Similar to setTimeout JS function."""
    timeout = TimeoutThread(callback, ms, args)
    timeout.start()
    return timeout.stop


class SyncPlayManager:
    def __init__(self, manager):
        self.playerManager = manager
        self.player = manager._player
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

    # On playback time update (call from timeline push)
    def sync_playback_time(self):
        if not self.last_command or self.last_command["Command"] != "Play" or self.is_buffering():
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

        current_position_ticks = int(self.player.playback_time * seconds_in_ticks)
        server_position_ticks = (self.last_command["PositionTicks"]
                                 + ((current_time - play_at_time) + self.time_offset)
                                 .total_seconds() * seconds_in_ticks)

        diff_ms = (server_position_ticks - current_position_ticks) / 10000
        self.playback_diff_ms = diff_ms

        if self.sync_enabled:
            abs_diff_ms = abs(diff_ms)

            if self.enable_speed_sync and settings.sync_max_delay_speed < abs_diff_ms < settings.sync_method_thresh:
                if self.attempts > settings.sync_speed_attempts:
                    self.enable_speed_sync = False
                    return

                # Speed To Sync Method
                speed = 1 + diff_ms / settings.sync_speed_time

                self.player.speed = speed
                self.sync_enabled = False
                self.attempts += 1
                log.info("SyncPlay Speed to Sync rate: {0}".format(speed))
                self.player_message("SpeedToSync (x{0})".format(speed))

                def callback():
                    self.player.speed = 1
                    self.sync_enabled = True
                set_timeout(settings.sync_speed_time, callback)
            elif abs_diff_ms > settings.sync_max_delay_skip:
                if self.attempts > settings.sync_attempts:
                    self.sync_enabled = False
                    log.info("SyncPlay Sync Disabled due to too many attempts.")
                    self.player_message("Sync Disabled (Too Many Attempts)")
                    return

                # Skip To Sync Method
                self.local_seek(server_position_ticks / seconds_in_ticks)
                self.sync_enabled = False
                self.attempts += 1
                log.info("SyncPlay Skip to Sync Activated")
                self.player_message("SkipToSync (x{0})".format(self.attempts))

                def callback():
                    self.sync_enabled = True
                set_timeout(settings.sync_method_thresh / 2, callback)
            else:
                if self.attempts > 0:
                    log.info("Playback synced after {0} attempts.".format(self.attempts))
                self.attempts = 0

    # On timesync update
    def on_timesync_update(self, time_offset, ping):
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
                self.client.jellyfin.ping_sync_play(ping.total_seconds() * 1000)
            except Exception:
                log.error("Syncplay ping reporting failed.", exc_info=True)

    def enable_sync_play(self, start_time, from_server):
        self.playback_rate = self.player.speed
        self.enabled_at = start_time
        self.enable_speed_sync = True

        def ready_callback():
            self.process_command(self.queued_command)
            self.queued_command = None
        self.read_callback = ready_callback

        self.ready = False
        self.notify_sync_ready = True

        self.client = self.playerManager._video.client
        timesync = self.client.timesync
        if self.timesync is not None and timesync is not self.timesync:
            self.timesync.remove_subscriber(self.on_timesync_update)
            self.timesync.stop_ping()

        self.timesync = timesync
        self.timesync.subscribe_time_offset(self.on_timesync_update)
        self.timesync.force_update()

        log.info("Syncplay enabled.")
        if from_server:
            self.player_message("SyncPlay enabled.")

    def disable_sync_play(self, from_server):
        self.player.speed = self.playback_rate

        self.enabled_at = None
        self.ready = False
        self.last_command = None
        self.queued_command = None
        self.sync_enabled = False

        if self.timesync is not None:
            self.timesync.remove_subscriber(self.on_timesync_update)
            self.timesync.stop_ping()
            self.timesync = None
        self.current_group = None

        log.info("Syncplay disabled.")
        if from_server:
            self.player_message("SyncPlay disabled.")

    # On Buffer
    def on_buffer(self):
        # TODO: Implement group wait.
        if not self.last_playback_waiting:
            self.last_playback_waiting = datetime.utcnow()

    # On Buffer Done
    def on_buffer_done(self):
        # TODO: Implement group wait.
        self.last_playback_waiting = None

    def is_buffering(self):
        if self.last_playback_waiting is None:
            return False
        return (datetime.utcnow() - self.last_playback_waiting).total_seconds() * 1000 > self.min_buffer_thresh_ms

    def is_enabled(self):
        return self.enabled_at is not None

    def process_group_update(self, command):
        command_type = command["Type"]
        log.debug("Syncplay group update: {0}".format(command))
        if command_type in info_commands:
            self.player_message(info_commands[command_type])
        elif command_type == "PrepareSession":
            self.prepare_session(command["GroupId"], command["Data"])
        elif command_type == "GroupJoined":
            self.current_group = command["GroupId"]
            self.enable_sync_play(_parse_precise_time(command["Data"]), True)
        elif command_type == "GroupLeft" or command_type == "NotInGroup":
            self.disable_sync_play(True)
        elif command_type == "UserJoined":
            self.player_message("{0} has joined.".format(command["Data"]))
        elif command_type == "UserLeft":
            self.player_message("{0} has left.".format(command["Data"]))
        elif command_type == "GroupWait":
            self.player_message("{0} is buffering.".format(command["Data"]))
        else:
            log.error("Unknown SyncPlay command {0} payload {1}.".format(command_type, command))

    def process_command(self, command):
        if command is None:
            return

        if not self.is_enabled():
            log.debug("Ignoring command {0} due to SyncPlay being disabled.".format(command))
            return

        if not self.ready:
            log.debug("Queued command {0} due to SyncPlay not being ready.".format(command))
            self.queued_command = command
            return

        command["When"] = _parse_precise_time(command["When"])
        command["EmitttedAt"] = _parse_precise_time(command["EmittedAt"])

        if command["EmitttedAt"] < self.enabled_at:
            log.debug("Ignoring old command {0}.".format(command))
            return

        if (self.last_command and
                self.last_command["When"] == command["When"] and
                self.last_command["PositionTicks"] == command["PositionTicks"] and
                self.last_command["Command"] == command["Command"]):
            log.debug("Ignoring duplicate command {0}.".format(command))

        self.last_command = command
        command_cmd, when, position = command["Command"], command["When"], command["PositionTicks"]
        log.info("Syncplay will {0} at {1} position {2}".format(command_cmd, when, position))

        if command_cmd == "Play":
            self.schedule_play(when, position)
        elif command_cmd == "Pause":
            self.schedule_pause(when, position)
        elif command_cmd == "Seek":
            self.schedule_seek(when, position)
        else:
            log.error("Command {0} is unknown.".format(command_cmd))

    def prepare_session(self, group_id, session_data):
        play_command = session_data.get('PlayCommand')
        if not self.playerManager._video:
            play_command = "PlayNow"

        seq = session_data.get("StartIndex")
        if seq is None:
            seq = 0
        media = Media(self.client, session_data.get("ItemIds"), seq=seq, user_id=session_data.get("ControllingUserId"),
                      aid=session_data.get("AudioStreamIndex"), sid=session_data.get("SubtitleStreamIndex"),
                      srcid=session_data.get("MediaSourceId"))

        if (self.playerManager._video and
                self.playerManager._video.item_id == session_data["ItemIds"][0] and play_command == "PlayNow"):
            # We assume the video is already available.
            self.playerManager._video.parent = media
            log.info("Syncplay Session Prepare: {0} {1}".format(group_id, session_data))
            self.local_seek((session_data.get("PositionTicks", 0) or 0) / seconds_in_ticks)
            self.current_group = group_id
        elif play_command == "PlayNow":
            log.info("Syncplay Session Recreate: {0} {1}".format(group_id, session_data))
            offset = session_data.get('StartPositionTicks')
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
            self.playerManager._video.parent.insert_items(session_data.get("ItemIds"), append=True)
            self.playerManager.upd_player_hide()
        elif play_command == "PlayNext":
            self.playerManager._video.parent.insert_items(session_data.get("ItemIds"), append=False)
            self.playerManager.upd_player_hide()

    def player_message(self, message):
        # Messages overwrite menu, so they are ignored.
        if not self.menu.is_menu_shown:
            if settings.sync_osd_message:
                self.player.show_text(message)
            else:
                log.info("SyncPlay Message: {0}".format(message))
        else:
            log.info("Ignored SyncPlay Message (menu): {0}".format(message))

    def schedule_play(self, when, position):
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
                self.sync_timeout = set_timeout(settings.sync_method_thresh / 2, sync_timeout)
            self.scheduled_command = set_timeout(play_timeout, scheduled)
        else:
            log.debug("SyncPlay Scheduled Play: Playing Now")
            # Group playback already started
            server_position_secs = position / seconds_in_ticks + (current_time - local_play_time).total_seconds()
            self.local_play()
            self.local_seek(server_position_secs)

            def sync_timeout():
                self.sync_enabled = True
            self.sync_timeout = set_timeout(settings.sync_method_thresh / 2, sync_timeout)

    def schedule_pause(self, when, position, seek_only=False):
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

    def schedule_seek(self, when, position):
        # This replicates what the web client does.
        self.schedule_pause(when, position)

    def clear_scheduled_command(self):
        if self.scheduled_command is not None:
            self.scheduled_command()

        if self.sync_timeout is not None:
            self.sync_timeout()

        self.sync_enabled = False
        self.player.speed = 1

    def play_request(self):
        self.client.jellyfin.play_sync_play()

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

    def join_group(self, group_id):
        self.client.jellyfin.join_sync_play(group_id)

    def menu_join_group(self):
        group = self.menu.menu_list[self.menu.menu_selection][2]
        self.menu.hide_menu()

        self.client.jellyfin.join_sync_play(group["GroupId"])
        self.local_seek(group["PositionTicks"] / seconds_in_ticks)

    def menu_disable(self):
        self.menu.hide_menu()
        self.client.jellyfin.leave_sync_play()

    def menu_create_group(self):
        self.menu.hide_menu()
        self.client.jellyfin.new_sync_play()

    def menu_action(self):
        self.client = self.playerManager._video.client

        selected = 0
        offset = 1
        group_option_list = [
            ("None (Disabled)", self.menu_disable, None),
        ]
        if not self.is_enabled():
            offset = 2
            group_option_list.append(("New Group", self.menu_create_group, None))
        groups = self.client.jellyfin.get_sync_play(self.playerManager._video.item_id)
        for i, group in enumerate(groups):
            group_option_list.append(
                (group["PlayingItemName"], self.menu_join_group, group)
            )
            if group["GroupId"] == self.current_group:
                selected = i + offset
        self.menu.put_menu("SyncPlay", group_option_list, selected)
