import logging
import urllib.parse
import os.path
import re
import pathlib
from io import BytesIO
from sys import platform

from .conf import settings
from .utils import is_local_domain, get_profile, get_seq
from .i18n import _

log = logging.getLogger("media")

from typing import TYPE_CHECKING, Optional, List

if TYPE_CHECKING:
    from jellyfin_apiclient_python import JellyfinClient as JellyfinClient_type


class Intro(object):
    def __init__(self, type, start, end):
        self.type: str = type  # "Intro" or "Outro"
        self.start: float = start
        self.end: float = end
        self.has_triggered: bool = False


class Video(object):
    def __init__(
        self,
        item_id: str,
        parent: "Media",
        aid: Optional[int] = None,
        sid: Optional[int] = None,
        srcid: Optional[str] = None,
    ):
        self.item_id = item_id
        self.parent = parent
        self.client = parent.client
        self.aid = aid
        self.sid = sid
        self.item = self.client.jellyfin.get_item(item_id)

        self.is_tv = self.item.get("Type") == "Episode"

        self.subtitle_seq = {}
        self.subtitle_uid = {}
        self.subtitle_url = {}
        self.subtitle_enc = set()
        self.audio_seq = {}
        self.audio_uid = {}
        self.is_transcode = False
        self.trs_ovr = None
        self.playback_info = None
        self.media_source = None
        self.srcid = srcid
        self.intros: List[Intro] = []
        self.intro_tried = False

    def map_streams(self):
        self.subtitle_seq = {}
        self.subtitle_uid = {}
        self.subtitle_url = {}
        self.subtitle_enc = set()
        self.audio_seq = {}
        self.audio_uid = {}

        if self.media_source is None or self.media_source["Protocol"] != "File":
            return

        index = 1
        for stream in self.media_source["MediaStreams"]:
            if stream.get("Type") != "Audio":
                continue

            self.audio_uid[index] = stream["Index"]
            self.audio_seq[stream["Index"]] = index

            if not stream.get("IsExternal"):
                index += 1

        index = 1
        for sub in self.media_source["MediaStreams"]:
            if sub.get("Type") != "Subtitle":
                continue

            if sub.get("DeliveryMethod") == "Embed":
                self.subtitle_uid[index] = sub["Index"]
                self.subtitle_seq[sub["Index"]] = index
            elif sub.get("DeliveryMethod") == "External":
                url = sub.get("DeliveryUrl")
                if not sub.get("IsExternalUrl"):
                    url = self.client.config.data["auth.server"] + url
                self.subtitle_url[sub["Index"]] = url
            elif sub.get("DeliveryMethod") == "Encode":
                self.subtitle_enc.add(sub["Index"])

            if not sub.get("IsExternal"):
                index += 1

        user_aid = self.media_source.get("DefaultAudioStreamIndex")
        user_sid = self.media_source.get("DefaultSubtitleStreamIndex")

        if user_aid is not None and self.aid is None:
            self.aid = user_aid

        if user_sid is not None and self.sid is None:
            self.sid = user_sid

    def get_current_streams(self):
        return self.aid, self.sid

    def get_proper_title(self):
        if not hasattr(self, "_title"):
            title = self.item.get("Name")
            if (
                self.is_tv
                and self.item.get("IndexNumber") is not None
                and self.item.get("ParentIndexNumber") is not None
            ):
                episode_number = int(self.item.get("IndexNumber"))
                season_number = int(self.item.get("ParentIndexNumber"))
                series_name = self.item.get("SeriesName")
                title = "%s - s%de%.2d - %s" % (
                    series_name,
                    season_number,
                    episode_number,
                    title,
                )
            elif self.item.get("Type") == "Movie":
                year = self.item.get("ProductionYear")
                if year is not None:
                    title = "%s (%s)" % (title, year)
            setattr(self, "_title", title)
        return getattr(self, "_title") + (
            _(" (Transcode)") if self.is_transcode else ""
        )

    def set_trs_override(self, video_bitrate: Optional[int], force_transcode: bool):
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
            try:
                self.client.jellyfin.close_transcode(
                    self.client.config.data["app.device_id"]
                )
            except:
                log.warning("Terminating transcode failed.", exc_info=1)

    def _get_url_from_source(self):
        # Only use Direct Paths if:
        # - The media source supports direct paths.
        # - Direct paths are enabled in the config.
        # - The server is local or the override config is set.
        # - If there's a scheme specified or the path exists as a local file.
        if (
            (
                self.media_source.get("Protocol") == "Http"
                or self.media_source["SupportsDirectPlay"]
            )
            and settings.direct_paths
            and (settings.remote_direct_paths or self.parent.is_local)
        ):
            if platform.startswith("win32") or platform.startswith("cygwin"):
                # matches on SMB scheme
                match = re.search("(?:\\\\).+:.*@(.+)", self.media_source["Path"])
                if match:
                    # replace forward slash to backward slashes
                    log.debug("cleaned up credentials from path")
                    self.media_source["Path"] = str(
                        pathlib.Path("\\\\" + match.groups()[0])
                    )

            if urllib.parse.urlparse(self.media_source["Path"]).scheme:
                self.is_transcode = False
                log.debug("Using remote direct path.")
                # translate path for windows
                # if path is smb path in credential format for kodi and maybe linux \\username:password@mediaserver\foo,
                # translate it to mediaserver/foo
                return str(pathlib.Path(self.media_source["Path"]))
            else:
                # If there's no uri scheme, check if the file exixsts because it might not be mounted
                if os.path.isfile(self.media_source["Path"]):
                    log.debug("Using local direct path.")
                    self.is_transcode = False
                    return self.media_source["Path"]

        if self.media_source["SupportsDirectStream"]:
            self.is_transcode = False
            log.info("Using direct url.")
            query_params = {
                "static": "true",
                "MediaSourceId": self.media_source["Id"],
                "api_key": self.client.config.data["auth.token"],
            }

            if "LiveStreamId" in self.media_source:
                query_params["LiveStreamId"] = self.media_source["LiveStreamId"]

            query = urllib.parse.urlencode(query_params)

            return "%s/Videos/%s/stream?%s" % (
                self.client.config.data["auth.server"],
                self.item_id,
                query,
            )
        elif self.media_source["SupportsTranscoding"]:
            log.info("Using transcode url.")
            self.is_transcode = True
            return self.client.config.data["auth.server"] + self.media_source.get(
                "TranscodingUrl"
            )

    def get_best_media_source(self, preferred: Optional[str] = None):
        weight_selected = 0
        preferred_selected = None
        selected = None
        for media_source in self.playback_info["MediaSources"]:
            if media_source.get("Id") == preferred:
                preferred_selected = media_source
            # Prefer the highest bitrate file that will direct play.
            weight = (media_source.get("SupportsDirectPlay") or 0) * 50000 + (
                media_source.get("Bitrate") or 0
            ) / 1000
            if weight > weight_selected:
                weight_selected = weight
                selected = media_source
        if preferred_selected:
            return preferred_selected
        else:
            if preferred is not None:
                log.warning("Preferred media source is unplayable.")
            return selected

    def get_intro(self, media_source_id):
        if self.intro_tried:
            return
        self.intro_tried = True

        # provided by plugin
        try:
            skip_intro_data = self.client.jellyfin.media_segments(
                f"/{media_source_id}?includeSegmentTypes=Outro&includeSegmentTypes=Intro"
            )
            for intro in skip_intro_data["Items"]:
                self.intros.append(
                    Intro(
                        intro["Type"], # Intro or Outro
                        intro["StartTicks"] / 10000000,
                        intro["EndTicks"] / 10000000
                    )
                )
        except:
            log.warning(
                "Fetching intro data failed.",
                exc_info=1,
            )

    def get_current_intro(self, time):
        for intro in self.intros:
            if intro.start <= time and time <= intro.end:
                return intro.start <= time, intro
        return False, None

    def get_chapters(self):
        return [
            {"start": item["StartPositionTicks"] / 10000000, "name": item["Name"]}
            for item in self.item.get("Chapters", [])
            if item.get("ImageTag")
        ]

    def get_chapter_images(self, max_width=400, quality=90):
        for i, item in enumerate(self.item.get("Chapters", [])):
            data = BytesIO()
            self.client.jellyfin._get_stream(
                f"Items/{self.item_id}/Images/Chapter/{i}",
                data,
                {"tag": item["ImageTag"], "maxWidth": max_width, "quality": quality},
            )
            yield data.getvalue()

    def get_hls_tile_images(self, width, count):
        for i in range(0, count):
            data = BytesIO()
            self.client.jellyfin._get_stream(
                f"Videos/{self.item['Id']}/Trickplay/{width}/{i}.jpg?MediaSourceId={self.media_source['Id']}",
                data,
            )
            yield data.getvalue()

    def get_bif(self, prefer_width=320):
        manifest = self.item.get("Trickplay")
        if (
            manifest is not None
            and manifest.get(self.media_source["Id"]) is not None
            and len(manifest[self.media_source["Id"]]) > 0
        ):
            available_widths = [
                int(x) for x in manifest[self.media_source["Id"]].keys()
            ]

            if prefer_width is not None:
                width = min(available_widths, key=lambda x: abs(x - prefer_width))
            else:
                width = max(available_widths)

            return manifest[self.media_source["Id"]][str(width)]
        else:
            return None

    def get_playback_url(
        self,
        video_bitrate: Optional[int] = None,
        force_transcode: Optional[int] = False,
    ):
        """
        Returns the URL to use for the transcoded file.
        """
        self.terminate_transcode()

        if self.trs_ovr:
            video_bitrate, force_transcode = self.trs_ovr

        log.info(
            "Bandwidth: local={0}, bitrate={1}, force={2}".format(
                self.parent.is_local, video_bitrate, force_transcode
            )
        )
        profile = get_profile(not self.parent.is_local, video_bitrate, force_transcode)
        self.playback_info = self.client.jellyfin.get_play_info(
            self.item_id, profile, self.aid, self.sid, media_source_id=self.srcid
        )

        self.media_source = self.get_best_media_source(self.srcid)
        if (
            settings.skip_intro_always
            or settings.skip_intro_enable
            or settings.skip_credits_always
            or settings.skip_credits_enable
        ):
            self.get_intro(self.media_source["Id"])

        self.map_streams()
        url = self._get_url_from_source()

        # If there are more media sources and the default one fails, try all of them.
        if url is None and len(self.playback_info["MediaSources"]) > 1:
            log.warning("Selected media source is unplayable.")
            for media_source in self.playback_info["MediaSources"]:
                if media_source["Id"] != self.srcid:
                    self.media_source = media_source
                    self.map_streams()
                    url = self._get_url_from_source()
                    if url is not None:
                        break

        if settings.log_decisions:
            if len(self.playback_info["MediaSources"]) > 1:
                log.info("Full Playback Info: {0}".format(self.playback_info))
            log.info("Media Decision: {0}".format(self.media_source))
        return url

    def get_duration(self):
        ticks = self.item.get("RunTimeTicks")
        if ticks:
            return ticks / 10000000

    def set_played(self, watched: bool = True):
        self.client.jellyfin.item_played(self.item_id, watched)

    def set_streams(self, aid: Optional[int], sid: Optional[int]):
        need_restart = False

        if aid is not None and self.aid != aid:
            self.aid = aid
            if self.is_transcode:
                need_restart = True

        if sid is not None and self.sid != sid:
            self.sid = sid
            if sid in self.subtitle_enc:
                need_restart = True

        return need_restart

    def get_playlist_id(self):
        return self.parent.queue[self.parent.seq]["PlaylistItemId"]


class Media(object):
    def __init__(
        self,
        client: "JellyfinClient_type",
        queue: list,
        seq: int = 0,
        user_id: Optional[str] = None,
        aid: Optional[int] = None,
        sid: Optional[int] = None,
        srcid: Optional[str] = None,
        queue_override: bool = True,
    ):
        if queue_override:
            self.queue = [
                {"PlaylistItemId": "playlistItem{0}".format(get_seq()), "Id": id_num}
                for id_num in queue
            ]
        else:
            self.queue = queue
        self.client = client
        self.seq = seq
        self.user_id = user_id

        self.video = Video(self.queue[seq]["Id"], self, aid, sid, srcid)
        self.is_tv = self.video.is_tv
        self.is_local = is_local_domain(client)
        self.has_next = seq < len(queue) - 1
        self.has_prev = seq > 0

    def get_next(self):
        if self.has_next:
            return Media(
                self.client,
                self.queue,
                self.seq + 1,
                self.user_id,
                queue_override=False,
            )

    def get_prev(self):
        if self.has_prev:
            return Media(
                self.client,
                self.queue,
                self.seq - 1,
                self.user_id,
                queue_override=False,
            )

    def get_from_key(self, item_id: str):
        for i, video in enumerate(self.queue):
            if video["Id"] == item_id:
                return Media(
                    self.client, self.queue, i, self.user_id, queue_override=False
                )
        return None

    def get_video(self, index: int):
        if index == 0 and self.video:
            return self.video

        if index < len(self.queue):
            return Video(self.queue[index]["Id"], self)

        log.error("Media::get_video couldn't find video at index %s" % index)

    def insert_items(self, items, append: bool = False):
        items = [
            {"PlaylistItemId": "playlistItem{0}".format(get_seq()), "Id": id_num}
            for id_num in items
        ]
        if append:
            self.queue.extend(items)
        else:
            self.queue = (
                self.queue[0 : self.seq + 1] + items + self.queue[self.seq + 1 :]
            )
        self.has_next = self.seq < len(self.queue) - 1

    def replace_queue(self, sp_items, seq):
        """Update queue for SyncPlay.
        Returns None if the video is the same or a new Media if not."""
        if self.queue[self.seq]["Id"] == sp_items[seq]["Id"]:
            self.queue, self.seq = sp_items, seq
            return None
        else:
            return Media(self.client, sp_items, seq, self.user_id, queue_override=False)
