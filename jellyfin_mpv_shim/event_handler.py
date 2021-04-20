import logging
import os

from .conf import settings
from .media import Media
from .player import playerManager
from .timeline import timelineManager

log = logging.getLogger("event_handler")
bindings = {}

NAVIGATION_DICT = {
    "Back": "back",
    "Select": "ok",
    "MoveUp": "up",
    "MoveDown": "down",
    "MoveRight": "right",
    "MoveLeft": "left",
    "GoHome": "home",
    "GoToSettings": "home",
}

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jellyfin_apiclient_python import JellyfinClient as JellyfinClient_type


def bind(event_name: str):
    def decorator(func):
        bindings[event_name] = func
        return func

    return decorator


class EventHandler(object):
    mirror = None

    def handle_event(
        self,
        client: "JellyfinClient_type",
        event_name: str,
        arguments: dict,
    ):
        if event_name in bindings:
            log.debug("Handled Event {0}: {1}".format(event_name, arguments))
            bindings[event_name](self, client, event_name, arguments)
        else:
            log.debug("Unhandled Event {0}: {1}".format(event_name, arguments))

    @bind("Play")
    def play_media(self, client: "JellyfinClient_type", _event_name, arguments: dict):
        play_command = arguments.get("PlayCommand")
        if not playerManager.has_video():
            play_command = "PlayNow"

        if play_command == "PlayNow":
            seq = arguments.get("StartIndex")
            if seq is None:
                seq = 0
            media = Media(
                client,
                arguments.get("ItemIds"),
                seq=seq,
                user_id=arguments.get("ControllingUserId"),
                aid=arguments.get("AudioStreamIndex"),
                sid=arguments.get("SubtitleStreamIndex"),
                srcid=arguments.get("MediaSourceId"),
            )

            log.debug("EventHandler::playMedia %s" % media)
            offset = arguments.get("StartPositionTicks")
            if offset is not None:
                offset /= 10000000

            video = media.video
            if video:
                if settings.pre_media_cmd:
                    os.system(settings.pre_media_cmd)
                playerManager.play(video, offset)
                timelineManager.send_timeline()
                if arguments.get("SyncPlayGroup") is not None:
                    playerManager.syncplay.join_group(arguments["SyncPlayGroup"])
        elif play_command == "PlayLast":
            playerManager.get_video().parent.insert_items(
                arguments.get("ItemIds"), append=True
            )
            playerManager.upd_player_hide()
        elif play_command == "PlayNext":
            playerManager.get_video().parent.insert_items(
                arguments.get("ItemIds"), append=False
            )
            playerManager.upd_player_hide()

    @bind("GeneralCommand")
    def general_command(
        self, client: "JellyfinClient_type", _event_name, arguments: dict
    ):
        command = arguments.get("Name")
        if command == "SetVolume":
            # There is currently a bug that causes this to be spammed, so we
            # only update it if the value actually changed.
            if playerManager.get_volume(True) != int(arguments["Arguments"]["Volume"]):
                playerManager.set_volume(int(arguments["Arguments"]["Volume"]))
        elif command == "SetAudioStreamIndex":
            playerManager.set_streams(int(arguments["Arguments"]["Index"]), None)
        elif command == "SetSubtitleStreamIndex":
            playerManager.set_streams(None, int(arguments["Arguments"]["Index"]))
        elif command == "DisplayContent":
            # If you have an idle command set, this will delay it.
            timelineManager.delay_idle()
            if self.mirror:
                self.mirror.display_content(client, arguments)
        elif command in (
            "Back",
            "Select",
            "MoveUp",
            "MoveDown",
            "MoveRight",
            "MoveLeft",
            "GoHome",
            "GoToSettings",
        ):
            playerManager.menu_action(NAVIGATION_DICT[command])
        elif command in ("Mute", "Unmute"):
            playerManager.set_mute(command == "Mute")
        elif command == "TakeScreenshot":
            playerManager.screenshot()
        elif command == "ToggleFullscreen" or command is None:
            # Currently when you hit the fullscreen button, no command is specified...
            playerManager.toggle_fullscreen()

    @bind("Playstate")
    def play_state(self, _client: "JellyfinClient_type", _event_name, arguments: dict):
        command = arguments.get("Command")
        if command == "PlayPause":
            playerManager.toggle_pause()
        elif command == "Pause":
            playerManager.pause_if_playing()
        elif command == "Unpause":
            playerManager.play_if_paused()
        elif command == "PreviousTrack":
            playerManager.play_prev()
        elif command == "NextTrack":
            playerManager.play_next()
        elif command == "Stop":
            playerManager.stop()
        elif command == "Seek":
            playerManager.seek(
                arguments.get("SeekPositionTicks") / 10000000, absolute=True
            )

    @bind("PlayPause")
    def pause_play(self, _client: "JellyfinClient_type", _event_name, _arguments: dict):
        playerManager.toggle_pause()
        timelineManager.send_timeline()

    @bind("SyncPlayGroupUpdate")
    def sync_play_group_update(
        self, client: "JellyfinClient_type", _event_name, arguments: dict
    ):
        playerManager.syncplay.client = client
        playerManager.syncplay.process_group_update(arguments)

    @bind("SyncPlayCommand")
    def sync_play_command(
        self, client: "JellyfinClient_type", _event_name, arguments: dict
    ):
        playerManager.syncplay.client = client
        playerManager.syncplay.process_command(arguments)


eventHandler = EventHandler()
