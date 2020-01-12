import logging
import threading
import time
import os

from .conf import settings
from .player import playerManager
from .utils import Timer, mpv_color_to_plex

log = logging.getLogger("timeline")

class TimelineManager(threading.Thread):
    def __init__(self):
        self.currentItems   = {}
        self.currentStates  = {}
        self.idleTimer      = Timer()
        self.stopped        = False
        self.halt           = False
        self.trigger        = threading.Event()
        self.is_idle        = True
        self.last_video     = None
        self.client = None

        threading.Thread.__init__(self)

    def stop(self):
        self.halt = True
        self.join()

    def run(self):
        force_next = False
        while not self.halt:
            if (playerManager._player and playerManager._video) or force_next:
                if not playerManager.is_paused() or force_next:
                    self.SendTimeline()
                self.delay_idle()
            force_next = False
            if self.idleTimer.elapsed() > settings.idle_cmd_delay and not self.is_idle and settings.idle_cmd:
                os.system(settings.idle_cmd)
                self.is_idle = True
            if self.trigger.wait(1):
                force_next = True
                self.trigger.clear()

    def delay_idle(self):
        self.idleTimer.restart()
        self.is_idle = False

    def SendTimeline(self):
        if self.client is not None:
            pass

    def GetCurrentTimeline(self):
        # https://github.com/plexinc/plex-home-theater-public/blob/pht-frodo/plex/Client/PlexTimelineManager.cpp#L142
        # Note: location is set to "" to avoid pop-up of navigation menu. This may be abuse of the API.
        options = {
            "location": "",
            "state":    playerManager.get_state(),
            "type":     "video"
        }
        controllable = []

        video  = playerManager._video
        player = playerManager._player

        # The playback_time value can take on the value of none, probably
        # when playback is complete. This avoids the thread crashing.
        if video and not player.playback_abort and player.playback_time:
            self.last_video = video
            media = playerManager._video.parent

            options["location"]          = "fullScreenVideo"
            options["time"]              = int(player.playback_time * 1e3)
            options["autoPlay"]          = '1' if settings.auto_play else '0'
            
            aid, sid = playerManager.get_track_ids()

            if aid:
                options["audioStreamID"] = aid
            if sid:
                options["subtitleStreamID"] = sid
                options["subtitleSize"] = settings.subtitle_size
                controllable.append("subtitleSize")
                
                if not video.is_transcode:
                    options["subtitlePosition"] = settings.subtitle_position
                    options["subtitleColor"] = mpv_color_to_plex(settings.subtitle_color)
                    controllable.append("subtitlePosition")
                    controllable.append("subtitleColor")

            options["ratingKey"]         = video.get_video_attr("ratingKey")
            options["key"]               = video.get_video_attr("key")
            options["containerKey"]      = video.get_video_attr("key")
            options["guid"]              = video.get_video_attr("guid")
            options["duration"]          = video.get_video_attr("duration", "0")
            options["address"]           = media.path.hostname
            options["protocol"]          = media.path.scheme
            options["port"]              = media.path.port
            options["machineIdentifier"] = media.get_machine_identifier()
            options["seekRange"]         = "0-%s" % options["duration"]

            if media.play_queue:
                options.update(media.get_queue_info())

            controllable.append("playPause")
            controllable.append("stop")
            controllable.append("stepBack")
            controllable.append("stepForward")
            controllable.append("seekTo")
            controllable.append("skipTo")
            controllable.append("autoPlay")

            controllable.append("subtitleStream")
            controllable.append("audioStream")

            if video.parent.has_next:
                controllable.append("skipNext")
            
            if video.parent.has_prev:
                controllable.append("skipPrevious")

            # If the duration is unknown, disable seeking
            if options["duration"] == "0":
                options.pop("duration")
                options.pop("seekRange")
                controllable.remove("seekTo")

            # Volume control is enabled only if output isn't HDMI,
            # although technically I'm pretty sure we can still control
            # the volume even if the output is hdmi...
            if settings.audio_output != "hdmi":
                controllable.append("volume")
                options["volume"] = str(playerManager.get_volume(percent=True)*100 or 0)

            options["controllable"] = ",".join(controllable)
        else:
            if self.last_video:
                video = self.last_video
                options["ratingKey"]         = video.get_video_attr("ratingKey")
                options["key"]               = video.get_video_attr("key")
                options["containerKey"]      = video.get_video_attr("key")
                if video.parent.play_queue:
                    options.update(video.parent.get_queue_info())
            if player.playback_abort:
                options["state"] = "stopped"
            else:
                options["state"] = "buffering"

        return options


timelineManager = TimelineManager()
