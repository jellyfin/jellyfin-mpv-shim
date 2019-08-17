import logging
import requests
import threading
import time

try:
    from xml.etree import cElementTree as et
except:
    from xml.etree import ElementTree as et

from io import BytesIO

from conf import settings
from player import playerManager
from subscribers import remoteSubscriberManager
from utils import Timer

log = logging.getLogger("timeline")

class TimelineManager(threading.Thread):
    def __init__(self):
        self.currentItems   = {}
        self.currentStates  = {}
        self.idleTimer      = Timer()
        self.subTimer       = Timer()
        self.serverTimer    = Timer()
        self.stopped        = False
        self.halt           = False

        threading.Thread.__init__(self)

    def stop(self):
        self.halt = True
        self.join()

    def run(self):
        while not self.halt:
            if playerManager._player and playerManager._video:
                if not playerManager.is_paused():
                    self.SendTimelineToSubscribers()
                playerManager.update()
                self.idleTimer.restart()
            time.sleep(1)

    def SendTimelineToSubscribers(self):
        log.debug("TimelineManager::SendTimelineToSubscribers updating all subscribers")
        for sub in list(remoteSubscriberManager.subscribers.values()):
            self.SendTimelineToSubscriber(sub)

    def SendTimelineToSubscriber(self, subscriber):
        if subscriber.url == "":
            return
        timelineXML = self.GetCurrentTimeLinesXML(subscriber)
        url = "%s/:/timeline" % subscriber.url

        log.debug("TimelineManager::SendTimelineToSubscriber sending timeline to %s" % url)

        tree = et.ElementTree(timelineXML)
        tmp  = BytesIO()
        tree.write(tmp, encoding="utf-8", xml_declaration=True)

        tmp.seek(0)
        xmlData = tmp.read()

        # TODO: Abstract this into a utility function and add other X-Plex-XXX fields
        requests.post(url, data=xmlData, headers={
            "Content-Type":             "application/x-www-form-urlencoded",
            "Connection":               "keep-alive",
            "Content-Range":            "bytes 0-/-1",
            "X-Plex-Client-Identifier": settings.client_uuid
        })

    def WaitForTimeline(self, subscriber):
        time.sleep(1)
        return self.GetCurrentTimeLinesXML(subscriber)

    def GetCurrentTimeLinesXML(self, subscriber):
        tlines = self.GetCurrentTimeline()

        #
        # Only "video" is supported right now
        #
        mediaContainer = et.Element("MediaContainer")
        if subscriber.commandID is not None:
            mediaContainer.set("commandID", str(subscriber.commandID))
        mediaContainer.set("location", tlines["location"])

        lineEl = et.Element("Timeline")
        for key, value in list(tlines.items()):
            lineEl.set(key, str(value))
        mediaContainer.append(lineEl)

        return mediaContainer

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

        if video and not player.playback_abort:
            media = playerManager._video.parent

            options["location"]          = "fullScreenVideo"

            options["time"]              = player.playback_time * 1e3
            if player.sub != 'no':
                options["subtitleStreamID"] = video.subtitle_uid.get(player.sub, '')

            if player.audio != 'no':
                options["audioStreamID"] = video.audio_uid.get(player.audio, '')

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

            controllable.append("playPause")
            controllable.append("stop")
            controllable.append("stepBack")
            controllable.append("stepForward")
            controllable.append("subtitleStream")
            controllable.append("audioStream")
            controllable.append("seekTo")

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
            options["time"] = 0

        return options


timelineManager = TimelineManager()
