import logging
import requests
import uuid

from .conf import settings
from .utils import is_local_domain, get_profile

log = logging.getLogger('media')

class Video(object):
    def __init__(self, item_id, parent, aid=None, sid=None):
        self.item_id       = item_id
        self.parent        = parent
        self.client        = parent.client
        self.aid           = aid
        self.sid           = sid
        self.item          = client.jellyfin.get_item(item_id)

        self.is_tv = self.item.get("Type") == "Episode"

        self.subtitle_seq  = {}
        self.subtitle_uid  = {}
        self.audio_seq     = {}
        self.audio_uid     = {}
        self.is_transcode  = False
        self.trs_ovr       = None

        self.map_streams()

    def map_streams(self):
        if self.item.get("VideoType") != "VideoFile":
            return

        index = 1
        for stream in self.item["MediaSources"][0]["MediaStreams"]:
            if stream.get("Type") != "Audio" and stream.get("IsExternal") == False:
                continue

            self.audio_uid[index] = stream["Index"]
            self.audio_seq[stream["Index"]] = index
            index += 1

        if self.item.get("HasSubtitles"):
            index = 1
            for sub in self.item["MediaSources"][0]["MediaStreams"]:
                if stream.get("Type") != "Subtitle" and stream.get("IsExternal") == False:
                    continue

                self.subtitle_uid[index] = sub["Index"]
                self.subtitle_seq[sub["Index"]] = index

    def get_current_streams(self):
        return self.aid, self.sid

    def get_proper_title(self):
        if not hasattr(self, "_title"):
            title = self.item.get("Name")
            if self.is_tv:
                episode_number = int(self.item.get("IndexNumber"))
                season_number  = int(self.item.get("ParentIndexNumber"))
                series_name    = self.item.get("SeriesName")
                title = "%s - s%de%.2d - %s" % (series_name, season_number, episode_number, title)
            elif self.item.get("Type") == "Movie"
                year  = self.item.get("ProductionYear")
                if year is not None:
                    title = "%s (%s)" % (title, year)
            setattr(self, "_title", title)
        return getattr(self, "_title")

    def set_trs_override(self, video_bitrate, force_transcode):
        if force_transcode:
            self.trs_ovr = (video_bitrate, force_transcode)
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
        elif self.parent.is_local:
            return "max"
        else:
            return settings.remote_kbps

    def terminate_transcode(self):
        if self.is_transcode:
            client.jellyfin.close_transcode(client.config.data["app.device_id"])

    def get_playback_url(self, offset=0, video_bitrate=None, force_transcode=False, force_bitrate=False):
        """
        Returns the URL to use for the trancoded file.
        """
        self.terminate_transcode()

        if self.trs_ovr:
            video_bitrate, force_transcode = self.trs_ovr
        
        profile = get_profile(not self.is_local, video_bitrate, force_transcode)

        

    def get_duration(self):
        ticks = self.item.get("RunTimeTicks")
        if ticks:
            return ticks / 10000000

    def set_played(self, watched=True):
        self.client.jellyfin.item_played(self.item_id, watched)

class Media(object):
    def __init__(self, client, queue, seq=0, user_id=None, aid=None, sid=None):
        self.queue = queue
        self.client = client
        self.seq = seq
        self.user_id = user_id

        self.video = Video(queue[seq], self, aid=None, sid=None)
        self.is_tv = self.video.is_tv
        self.is_local = is_local_domain(client)
        self.has_next = seq < len(queue) - 1
        self.has_prev = seq > 0

    def get_next(self):
        if self.has_next:
            return Media(self.client, self.queue, self.seq+1, self.user_id)
    
    def get_prev(self):
        if self.has_prev:
            return Media(self.client, self.queue, self.seq-1, self.user_id)

    def get_from_key(self, item_id):
        for i, video in enumerate(self.queue):
            if video == item_id:
                return Media(self.client, self.queue, i, self.user_id)
        return None

    def get_video(self, index):
        if index == 0 and self.video:
            return self.video
        
        if index < len(self.queue):
            return Video(queue[index], self)

        log.error("Media::get_video couldn't find video at index %s" % video)
