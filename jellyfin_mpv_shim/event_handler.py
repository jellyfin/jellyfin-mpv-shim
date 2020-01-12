import logging
from .utils import plex_color_to_mpv
from .conf import settings
from .media import Media
from .player import playerManager
from .timeline import timelineManager

log = logging.getLogger("event_handler")
bindings = {}

def bind(event_name):
    def decorator(func):
        bindings[event_name] = func
        return func
    return decorator

class EventHandler(object):
    def handle_event(self, client, event_name, arguments):
        if event_name in bindings:
            log.debug("Handled Event {0}: {1}".format(event_name, arguments))
            bindings[event_name](self, client, event_name, arguments)
        else:
            log.debug("Unhandled Event {0}: {1}".format(event_name, arguments))

    @bind("Play")
    def play_media(self, client, event_name, arguments):
        media = Media(client, arguments.get("ItemIds"), seq=0, user_id=arguments.get("ControllingUserId"),
                      aid=arguments.get("AudioStreamIndex"), sid=arguments.get("SubtitleStreamIndex"))

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

    def stop(self, client, event_name, arguments):
        playerManager.stop()
        timelineManager.SendTimeline()

    def pausePlay(self, client, event_name, arguments):
        playerManager.toggle_pause()
        timelineManager.SendTimeline()

    def skipNext(self, client, event_name, arguments):
        playerManager.play_next()

    def skipPrevious(self, client, event_name, arguments):
        playerManager.play_prev()

    def seekTo(self, client, event_name, arguments):
        offset = int(int(arguments.get("offset", 0))*1e-3)
        log.debug("EventHandler::seekTo offset %ss" % offset)
        playerManager.seek(offset)

    def skipTo(self, client, event_name, arguments):
        playerManager.skip_to(arguments["key"])

    def set(self, client, event_name, arguments):
        if "volume" in arguments:
            volume = arguments["volume"]
            log.debug("EventHandler::set settings volume to %s" % volume)
            playerManager.set_volume(float(volume)/100.0)
        if "autoPlay" in arguments:
            settings.auto_play = arguments["autoPlay"] == "1"
            settings.save()
        subtitle_settings_upd = False
        if "subtitleSize" in arguments:
            subtitle_settings_upd = True
            settings.subtitle_size = int(arguments["subtitleSize"])
        if "subtitlePosition" in arguments:
            subtitle_settings_upd = True
            settings.subtitle_position = arguments["subtitlePosition"]
        if "subtitleColor" in arguments:
            subtitle_settings_upd = True
            settings.subtitle_color = plex_color_to_mpv(arguments["subtitleColor"])
        if subtitle_settings_upd:
            settings.save()
            playerManager.update_subtitle_visuals()

    def setStreams(self, client, event_name, arguments):
        audioStreamID = None
        subtitleStreamID = None
        if "audioStreamID" in arguments:
            audioStreamID = arguments["audioStreamID"]
        if "subtitleStreamID" in arguments:
            subtitleStreamID = arguments["subtitleStreamID"]
        playerManager.set_streams(audioStreamID, subtitleStreamID)

    def refreshPlayQueue(self, client, event_name, arguments):
        playerManager._video.parent.upd_play_queue()
        timelineManager.SendTimelineToSubscribers()

    def mirror(self, client, event_name, arguments):
        timelineManager.delay_idle()

    def navigation(self, client, event_name, arguments):
        path = path.path
        if path in NAVIGATION_DICT:
            playerManager.menu.menu_action(NAVIGATION_DICT[path])

eventHandler = EventHandler()
