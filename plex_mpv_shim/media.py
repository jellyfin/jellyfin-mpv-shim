import logging
import urllib.request, urllib.parse, urllib.error
import urllib.parse
import requests
import uuid

try:
    import xml.etree.cElementTree as et
except:
    import xml.etree.ElementTree as et

from .conf import settings
from .utils import get_plex_url, safe_urlopen, is_local_domain, get_session, reset_session

log = logging.getLogger('media')

# http://192.168.0.12:32400/photo/:/transcode?url=http%3A%2F%2F127.0.0.1%3A32400%2F%3A%2Fresources%2Fvideo.png&width=75&height=75

class MediaItem(object):
    pass

class Video(object):
    def __init__(self, node, parent, media=0, part=0):
        self.parent        = parent
        self.node          = node
        self.played        = False
        self._media        = 0
        self._media_node   = None
        self._part         = 0
        self._part_node    = None
        self.subtitle_seq  = {}
        self.subtitle_uid  = {}
        self.audio_seq     = {}
        self.audio_uid     = {}
        self.is_transcode  = False
        self.trs_aid       = None
        self.trs_sid       = None
        self.trs_ovr       = None

        if media:
            self.select_media(media, part)

        if not self._media_node:
            self.select_best_media(part)

        self.map_streams()

    def map_streams(self):
        if not self._part_node:
            return

        for index, stream in enumerate(self._part_node.findall("./Stream[@streamType='2']") or []):
            self.audio_uid[index+1] = stream.attrib["id"]
            self.audio_seq[stream.attrib["id"]] = index+1

        for index, sub in enumerate(self._part_node.findall("./Stream[@streamType='3']") or []):
            if sub.attrib.get("key") is None:
                self.subtitle_uid[index+1] = sub.attrib["id"]
                self.subtitle_seq[sub.attrib["id"]] = index+1

    def get_transcode_streams(self):
        if not self.trs_aid:
            audio_obj = self._part_node.find("./Stream[@streamType='2'][@selected='1']")
            self.trs_aid = audio_obj.get("id") if audio_obj is not None else None
        if not self.trs_sid:
            subtitle_obj = self._part_node.find("./Stream[@streamType='3'][@selected='1']")
            self.trs_sid = subtitle_obj.get("id") if subtitle_obj is not None else None
        return self.trs_aid, self.trs_sid

    def select_best_media(self, part=0):
        """
        Nodes are accessed via XPath, which is technically 1-indexed, while
        Plex is 0-indexed.
        """
        # Select the best media based on resolution
        highest_res = 0
        best_node   = 0
        for i, node in enumerate(self.node.findall('./Media')):
            res = int(node.get('height', 0))*int(node.get('height', 0))
            if res > highest_res:
                highest_res = res
                best_node   = i

        log.debug("Video::select_best_media selected media %s" % best_node)

        self.select_media(best_node)

    def select_media(self, media, part=0):
        node = self.node.find('./Media[%s]' % (media+1))
        if node:
            self._media      = media
            self._media_node = node
            if self.select_part(part):
                log.debug("Video::select_media selected media %d" % media)
                return True

        log.error("Video::select_media error selecting media %d" % media)
        return False

    def select_part(self, part):
        if self._media_node is None:
            return False

        node = self._media_node.find('./Part[%s]' % (part+1))
        if node:
            self._part      = part
            self._part_node = node
            return True

        log.error("Video::select_media error selecting part %s" % part)
        return False

    def is_multipart(self):
        if not self._media_node:
            return False
        return len(self._media_node.findall("./Part")) > 1

    def set_streams(self, audio_uid, sub_uid):
        args = {"allParts": "1"}

        if audio_uid is not None:
            args["audioStreamID"] = audio_uid
            self.trs_aid = audio_uid

        if sub_uid is not None:
            args["subtitleStreamID"] = sub_uid
            self.trs_sid = sub_uid

        if self._part_node != None:
            partid = self._part_node.get("id")
            url = "/library/parts/{0}".format(partid)
            requests.put(get_plex_url(urllib.parse.urljoin(self.parent.server_url, url), args), data=None)

    def get_proper_title(self):
        if not hasattr(self, "_title"):
            media_type = self.node.get('type')

            if self.parent.tree.find(".").get("identifier") != "com.plexapp.plugins.library":
                # Plugin?
                title =  self.node.get('sourceTitle') or ""
                if title:
                    title += " - "
                title += self.node.get('title') or ""
            else:
                # Assume local media
                if media_type == "movie":
                    title = self.node.get("title")
                    year  = self.node.get("year")
                    if year is not None:
                        title = "%s (%s)" % (title, year)
                elif media_type == "episode":
                    episode_name   = self.node.get("title")
                    episode_number = int(self.node.get("index"))
                    season_number  = int(self.node.get("parentIndex"))
                    series_name    = self.node.get("grandparentTitle")
                    title = "%s - %dx%.2d - %s" % (series_name, season_number, episode_number, episode_name)
                else:
                    # "clip", ...
                    title = self.node.get("title")
            setattr(self, "_title", title)
        return getattr(self, "_title")

    def set_trs_override(self, video_bitrate, force_transcode, force_bitrate):
        if force_transcode:
            self.trs_ovr = (video_bitrate, force_transcode, force_bitrate)
        else:
            self.trs_ovr = None

    def get_transcode_bitrate(self):
        if not self.is_transcode:
            return "none"
        elif self.trs_ovr is not None:
            if self.trs_ovr[0] is not None:
                return self.trs_ovr[0]
            elif self.trs_ovr[1]:
                return "max"
        elif is_local_domain(self.parent.path.hostname):
            return "max"
        else:
            return settings.transcode_kbps
        

    def get_formats(self):
        audio_formats = []
        protocols = "protocols=http-video,http-live-streaming,http-mp4-streaming,http-mp4-video,http-mp4-video-720p,http-streaming-video,http-streaming-video-720p;videoDecoders=mpeg4,h264{profile:high&resolution:1080&level:51};audioDecoders=mp3,aac{channels:8}"
        if settings.audio_ac3passthrough:
            audio_formats.append("add-transcode-target-audio-codec(type=videoProfile&context=streaming&protocol=hls&audioCodec=ac3)")
            audio_formats.append("add-transcode-target-audio-codec(type=videoProfile&context=streaming&protocol=hls&audioCodec=eac3)")
            protocols += ",ac3{bitrate:800000&channels:8}"
        if settings.audio_dtspassthrough:
            audio_formats.append("add-transcode-target-audio-codec(type=videoProfile&context=streaming&protocol=hls&audioCodec=dca)")
            protocols += ",dts{bitrate:800000&channels:8}"

        return audio_formats, protocols

    def is_transcode_suggested(self, video_bitrate=None, force_transcode=False, force_bitrate=False):
        request_direct_play = "1"
        request_subtitle_mode = "none"
        is_local = is_local_domain(self.parent.path.hostname)

        # User would like us to always transcode.
        if settings.always_transcode or force_transcode:
            request_direct_play = "0"
            request_subtitle_mode = "auto"
        # Check locally if we should transcode or direct play. (Legacy)
        elif (settings.remote_transcode and not settings.auto_transcode and not is_local
              and int(self.node.find("./Media").get("bitrate")) > settings.remote_kbps_thresh):
            request_direct_play = "0"
            request_subtitle_mode = "auto"

        # Regardless of if we need the data from the decision, Plex will sometimes deny access
        # if there is no decision for the current session.
        audio_formats, protocols = self.get_formats()

        url = "/video/:/transcode/universal/decision"
        args = {
            "hasMDE":             "1",
            "path":               self.node.get("key"),
            "session":            get_session(self.parent.path.hostname),
            "protocol":           "hls",
            "directPlay":         request_direct_play,
            "directStream":       "1",
            "fastSeek":           "1",
            "mediaIndex":         self._media or 0,
            "partIndex":          self._part or 0,
            "location":           "lan" if is_local else "wan",
            "autoAdjustQuality":  str(int(settings.adaptive_transcode)),
            "directStreamAudio":  "1",
            "subtitles":          request_subtitle_mode, # Setting this to auto or burn breaks direct play.
            "copyts":             "1",
        }

        if video_bitrate is not None and (not is_local or force_bitrate):
            args["maxVideoBitrate"] = str(video_bitrate)

        if audio_formats:
            args["X-Plex-Client-Profile-Extra"] = "+".join(audio_formats)
            args["X-Plex-Client-Capabilities"]  = protocols

        tree = et.parse(urllib.request.urlopen(get_plex_url(urllib.parse.urljoin(self.parent.server_url, url), args)))
        treeRoot = tree.getroot()
        decisionText = treeRoot.get("generalDecisionText") or treeRoot.get("mdeDecisionText")
        decision = treeRoot.get("generalDecisionCode") or treeRoot.get("mdeDecisionCode")
        log.debug("Decision: {0}: {1}".format(decision, decisionText))

        if request_direct_play == "0":
            return True
        # Use the decision from the Plex server.
        elif settings.auto_transcode:
            if decision == "1000":
                return False
            elif decision == "1001":
                return True
            else:
                log.error("Server reports that file cannot be streamed.")
        return False

    def get_playback_url(self, direct_play=None, offset=0, video_bitrate=None, force_transcode=False, force_bitrate=False):
        """
        Returns the URL to use for the trancoded file.
        """
        reset_session(self.parent.path.hostname)

        if self.trs_ovr:
            video_bitrate, force_transcode, force_bitrate = self.trs_ovr
        elif video_bitrate is None:
            video_bitrate = settings.transcode_kbps

        if direct_play is None:
            # See if transcoding is suggested
            direct_play = not self.is_transcode_suggested(video_bitrate, force_transcode, force_bitrate)

        if direct_play:
            if not self._part_node:
                return
            self.is_transcode = False
            url  = urllib.parse.urljoin(self.parent.server_url, self._part_node.get("key", ""))
            return get_plex_url(url)

        self.is_transcode = True
        is_local = is_local_domain(self.parent.path.hostname)

        url = "/video/:/transcode/universal/start.m3u8"
        args = {
            "path":               self.node.get("key"),
            "session":            get_session(self.parent.path.hostname),
            "protocol":           "hls",
            "directPlay":         "0",
            "directStream":       "1",
            "fastSeek":           "1",
            "mediaIndex":         self._media or 0,
            "partIndex":          self._part or 0,
            "location":           "lan" if is_local else "wan",
            "offset":             offset,
            "autoAdjustQuality":  str(int(settings.adaptive_transcode)),
            "directStreamAudio":  "1",
            "subtitles":          "auto",
            "copyts":             "1",
            #"skipSubtitles":    "1",
        }

        if video_bitrate is not None and (not is_local or force_bitrate):
            args["maxVideoBitrate"] = str(video_bitrate)

        audio_formats, protocols = self.get_formats()

        if audio_formats:
            args["X-Plex-Client-Profile-Extra"] = "+".join(audio_formats)
            args["X-Plex-Client-Capabilities"]  = protocols

        return get_plex_url(urllib.parse.urljoin(self.parent.server_url, url), args)

    def get_audio_idx(self):
        """
        Returns the index of the selected stream
        """
        if not self._part_node:
            return

        match = False
        index = None
        for index, stream in enumerate(self._part_node.findall("./Stream[@streamType='2']") or []):
            if stream.get('selected') == "1":
                match = True
                break

        if match:
            return index+1


    def get_subtitle_idx(self):
        if not self._part_node:
            return

        match = False
        index = None
        for index, sub in enumerate(self._part_node.findall("./Stream[@streamType='3']") or []):
            if sub.get('selected') == "1" and sub.get('key') is None:
                match = True
                break
        
        if match:
            return index+1

    def get_external_sub_id(self):
        if not self._part_node:
            return

        for sub in self._part_node.findall("./Stream[@streamType='3']") or []:
            if sub.get('selected') == "1" and sub.get('key') is not None:
                return sub.get("id")

    def get_external_sub(self, id):
        url = "/library/streams/{0}".format(id)
        return get_plex_url(urllib.parse.urljoin(self.parent.server_url, url))

    def get_duration(self):
        return self.node.get("duration")

    def get_rating_key(self):
        return self.node.get("ratingKey")

    def get_video_attr(self, attr, default=None):
        return self.node.get(attr, default)

    def update_position(self, ms):
        """
        Sets the state of the media as "playing" with a progress of ``ms`` milliseconds.
        """
        rating_key = self.get_rating_key()

        if rating_key is None:
            log.error("No 'ratingKey' could be found in XML from URL '%s'" % (self.parent.path.geturl()))
            return False

        url  = urllib.parse.urljoin(self.parent.server_url, '/:/progress')
        data = {
            "key":          rating_key,
            "time":         int(ms),
            "identifier":   "com.plexapp.plugins.library",
            "state":        "playing"
        }
        
        return safe_urlopen(url, data)

    def set_played(self, watched=True):
        rating_key = self.get_rating_key()

        if rating_key is None:
            log.error("No 'ratingKey' could be found in XML from URL '%s'" % (self.parent.path.geturl()))
            return False

        if watched:
            act = '/:/scrobble'
        else:
            act = '/:/unscrobble'

        url  = urllib.parse.urljoin(self.parent.server_url, act)
        data = {
            "key":          rating_key,
            "identifier":   "com.plexapp.plugins.library"
        }

        self.played = safe_urlopen(url, data)
        return self.played

class XMLCollection(object):
    def __init__(self, url):
        """
        ``url`` should be a URL to the Plex XML media item.
        """
        self.path       = urllib.parse.urlparse(url)
        self.server_url = self.path.scheme + "://" + self.path.netloc
        self.tree       = et.parse(urllib.request.urlopen(get_plex_url(url)))

    def get_path(self, path):
        return urllib.parse.urlunparse((self.path.scheme, self.path.netloc, path,
            self.path.params, self.path.query, self.path.fragment))

    def __str__(self):
        return self.path.path

class Media(XMLCollection):
    def __init__(self, url, series=None, seq=None, play_queue=None, play_queue_xml=None):
        XMLCollection.__init__(self, url)
        self.video = self.tree.find('./Video')
        self.is_tv = self.video.get("type") == "episode"
        self.seq = None
        self.has_next = False
        self.has_prev = False
        self.play_queue = play_queue
        self.play_queue_xml = play_queue_xml

        if self.play_queue:
            if not series:
                self.upd_play_queue()
            else:
                self.series = series
                self.seq = seq
                self.has_next = self.seq < len(self.series)
                self.has_prev = self.seq > 0
        elif self.is_tv:
            if series:
                self.series = series
                self.seq = seq
            else:
                self.series = []
                specials = []
                series_xml = XMLCollection(self.get_path(self.video.get("grandparentKey")+"/allLeaves"))
                videos = series_xml.tree.findall('./Video')
                
                # This part is kind of nasty, so we only try to do it once per cast session.
                key = self.video.get('key')
                is_special = False
                for i, video in enumerate(videos):
                    if video.get('key') == key:
                        self.seq = i
                        is_special = video.get('parentIndex') == '0'
                    if video.get('parentIndex') == '0':
                        specials.append(video)
                    else:
                        self.series.append(video)
                if is_special:
                    self.seq += len(self.series)
                else:
                    self.seq -= len(specials)
                self.series.extend(specials)
            self.has_next = self.seq < len(self.series)
            self.has_prev = self.seq > 0

    def upd_play_queue(self):
        if self.play_queue:
            self.play_queue_xml = XMLCollection(self.get_path(self.play_queue))
            videos = self.play_queue_xml.tree.findall('./Video')
            self.series = []

            key = self.video.get('key')
            for i, video in enumerate(videos):
                if video.get('key') == key:
                    self.seq = i
                self.series.append(video)

            self.has_next = self.seq < len(self.series)
            self.has_prev = self.seq > 0

    def get_queue_info(self):
        return {
            "containerKey": self.play_queue,
            "playQueueID": self.play_queue_xml.tree.find(".").get("playQueueID"),
            "playQueueVersion": self.play_queue_xml.tree.find(".").get("playQueueVersion"),
            "playQueueItemID": self.series[self.seq].get("playQueueItemID")
        }

    def get_next(self):
        if self.has_next:
            if self.play_queue and self.seq+1 == len(self.series):
                self.upd_play_queue()
            next_video = self.series[self.seq+1]
            return Media(self.get_path(next_video.get('key')), self.series, self.seq+1, self.play_queue, self.play_queue_xml)
    
    def get_prev(self):
        if self.has_prev:
            if self.play_queue and self.seq-1 == 0:
                self.upd_play_queue()
            prev_video = self.series[self.seq-1]
            return Media(self.get_path(prev_video.get('key')), self.series, self.seq-1, self.play_queue, self.play_queue_xml)

    def get_from_key(self, key):
        if self.play_queue:
            self.upd_play_queue()
            for i, video in enumerate(self.series):
                if video.get("key") == key:
                    return Media(self.get_path(key), self.series, i, self.play_queue, self.play_queue_xml)
            return None
        else:
            return Media(self.get_path(key))

    def get_video(self, index, media=0, part=0):
        if index == 0 and self.video:
            return Video(self.video, self, media, part)
        
        video = self.tree.find('./Video[%s]' % (index+1))
        if video:
            return Video(video, self, media, part)

        log.error("Media::get_video couldn't find video at index %s" % video)

    def get_machine_identifier(self):
        if not hasattr(self, "_machine_identifier"):
            doc = urllib.request.urlopen(get_plex_url(self.server_url))
            tree = et.parse(doc)
            setattr(self, "_machine_identifier", tree.find('.').get("machineIdentifier"))
        return getattr(self, "_machine_identifier", None)

