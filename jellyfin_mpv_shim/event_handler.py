import logging
import os

from .utils import plex_color_to_mpv
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
}

def bind(event_name):
    def decorator(func):
        bindings[event_name] = func
        return func
    return decorator

class EventHandler(object):
    mirror = None

    def handle_event(self, client, event_name, arguments):
        if event_name in bindings:
            log.debug("Handled Event {0}: {1}".format(event_name, arguments))
            bindings[event_name](self, client, event_name, arguments)
        else:
            log.debug("Unhandled Event {0}: {1}".format(event_name, arguments))

    @bind("Play")
    def play_media(self, client, event_name, arguments):
        play_command = arguments.get('PlayCommand')
        if not playerManager._video:
            play_command = "PlayNow"

        if play_command == "PlayNow":
            media = Media(client, arguments.get("ItemIds"), seq=0, user_id=arguments.get("ControllingUserId"),
                        aid=arguments.get("AudioStreamIndex"), sid=arguments.get("SubtitleStreamIndex"), srcid=arguments.get("MediaSourceId"))

            log.debug("EventHandler::playMedia %s" % media)
            offset = arguments.get('StartPositionTicks')
            if offset is not None:
                offset /= 10000000

            video = media.video
            if video:
                if settings.pre_media_cmd:
                    os.system(settings.pre_media_cmd)
                playerManager.play(video, offset)
                timelineManager.SendTimeline()
        elif play_command == "PlayLast":
            playerManager._video.parent.insert_items(arguments.get("ItemIds"), append=True)
            playerManager.upd_player_hide()
        elif play_command == "PlayNext":
            playerManager._video.parent.insert_items(arguments.get("ItemIds"), append=False)
            playerManager.upd_player_hide()

    @bind("GeneralCommand")
    def general_command(self, client, event_name, arguments):
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
                self.mirror.DisplayContent(client, arguments)
        elif command in ("Back", "Select", "MoveUp", "MoveDown", "MoveRight", "MoveRight", "GoHome"):
            playerManager.menu.menu_action(NAVIGATION_DICT[command])
        elif command in ("Mute", "Unmute"):
            playerManager.set_mute(command == "Mute")
        elif command == "TakeScreenshot":
            playerManager.screenshot()
        elif command == "ToggleFullscreen" or command is None:
            # Currently when you hit the fullscreen button, no command is specified...
            playerManager.toggle_fullscreen()

    @bind("Playstate")
    def play_state(self, client, event_name, arguments):
        command = arguments.get("Command")
        if command == "PlayPause":
            playerManager.toggle_pause()
        elif command == "PreviousTrack":
            playerManager.play_prev()
        elif command == "NextTrack":
            playerManager.play_next()
        elif command == "Stop":
            playerManager.stop()
        elif command == "Seek":
            playerManager.seek(arguments.get("SeekPositionTicks") / 10000000)

    @bind("PlayPause")
    def pausePlay(self, client, event_name, arguments):
        playerManager.toggle_pause()
        timelineManager.SendTimeline()

eventHandler = EventHandler()
