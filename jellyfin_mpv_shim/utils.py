import socket
import ipaddress
import requests
import urllib.parse
from threading import Lock
import logging
import sys
import os.path
import platform

from .conf import settings
from datetime import datetime
from functools import wraps
from .constants import USER_APP_NAME
from .i18n import _

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from jellyfin_apiclient_python import JellyfinClient as JellyfinClient_type

log = logging.getLogger("utils")

seq_num = 0
seq_num_lock = Lock()


class Timer(object):
    def __init__(self):
        self.started = datetime.now()

    def restart(self):
        self.started = datetime.now()

    def elapsed_ms(self):
        return self.elapsed() * 1e3

    def elapsed(self):
        return (datetime.now() - self.started).total_seconds()


def synchronous(tlockname: str):
    """
    A decorator to place an instance based lock around a method.
    From: http://code.activestate.com/recipes/577105-synchronization-decorator-for-class-methods/
    """

    def _synched(func):
        @wraps(func)
        def _synchronizer(self, *args, **kwargs):
            tlock = self.__getattribute__(tlockname)
            tlock.acquire()
            try:
                return func(self, *args, **kwargs)
            finally:
                tlock.release()

        return _synchronizer

    return _synched


def is_local_domain(client: "JellyfinClient_type"):
    # With Jellyfin, it is significantly more likely the user will be using
    # an address that is a hairpin NAT. We want to detect this and avoid
    # imposing limits in this case.
    url = client.config.data.get("auth.server", "")
    domain = urllib.parse.urlparse(url).hostname

    addr_info = socket.getaddrinfo(domain, 8096)[0]
    ip = addr_info[4][0]
    is_local = ipaddress.ip_address(ip).is_private

    if not is_local:
        if addr_info[0] == socket.AddressFamily.AF_INET:
            try:
                wan_ip = requests.get(
                    "https://checkip.amazonaws.com/", timeout=(3, 10)
                ).text.strip("\r\n")
                return ip == wan_ip
            except Exception:
                log.warning(
                    "checkip.amazonaws.com is unavailable. Assuming potential WAN ip is remote.",
                    exc_info=True,
                )
                return False
        elif addr_info[0] == socket.AddressFamily.AF_INET6:
            return False
    return True


def mpv_color_to_plex(color: str):
    return "#" + color.lower()[3:]


def plex_color_to_mpv(color: str):
    return "#FF" + color.upper()[1:]


def get_profile(
    is_remote: bool = False,
    video_bitrate: Optional[int] = None,
    force_transcode: bool = False,
    is_tv: bool = False,
):
    if video_bitrate is None:
        if is_remote:
            video_bitrate = settings.remote_kbps
        else:
            video_bitrate = settings.local_kbps

    if settings.force_video_codec:
        transcode_codecs = settings.force_video_codec
    elif settings.allow_transcode_to_h265 and not settings.transcode_hevc:
        transcode_codecs = "h264,h265,hevc,mpeg4,mpeg2video"
    elif settings.prefer_transcode_to_h265 and not settings.transcode_hevc:
        transcode_codecs = "h265,hevc,h264,mpeg4,mpeg2video"
    else:
        transcode_codecs = "h264,mpeg4,mpeg2video"

    if settings.force_audio_codec:
        audio_transcode_codecs = settings.force_audio_codec
    else:
        audio_transcode_codecs = "aac,mp3,ac3,opus,flac,vorbis"

    profile = {
        "Name": USER_APP_NAME,
        "MaxStreamingBitrate": video_bitrate * 1000,
        "MaxStaticBitrate": video_bitrate * 1000,
        "MusicStreamingTranscodingBitrate": 1280000,
        "TimelineOffsetSeconds": 5,
        "TranscodingProfiles": [
            {"Type": "Audio"},
            {
                "Container": "ts",
                "Type": "Video",
                "Protocol": "hls",
                "AudioCodec": audio_transcode_codecs,
                "VideoCodec": transcode_codecs,
                "MaxAudioChannels": "6",
            },
            {"Container": "jpeg", "Type": "Photo"},
        ],
        "DirectPlayProfiles": [{"Type": "Video"}, {"Type": "Audio"}, {"Type": "Photo"}],
        "ResponseProfiles": [],
        "ContainerProfiles": [],
        "CodecProfiles": [],
        "SubtitleProfiles": [
            {"Format": "srt", "Method": "External"},
            {"Format": "srt", "Method": "Embed"},
            {"Format": "ass", "Method": "External"},
            {"Format": "ass", "Method": "Embed"},
            {"Format": "sub", "Method": "Embed"},
            {"Format": "sub", "Method": "External"},
            {"Format": "ssa", "Method": "Embed"},
            {"Format": "ssa", "Method": "External"},
            {"Format": "smi", "Method": "Embed"},
            {"Format": "smi", "Method": "External"},
            # Jellyfin currently refuses to serve these subtitle types as external.
            {"Format": "pgssub", "Method": "Embed"},
            # {
            #    "Format": "pgssub",
            #    "Method": "External"
            # },
            {"Format": "dvdsub", "Method": "Embed"},
            {"Format": "dvbsub", "Method": "Embed"},
            # {
            #    "Format": "dvdsub",
            #    "Method": "External"
            # },
            {"Format": "pgs", "Method": "Embed"},
            # {
            #    "Format": "pgs",
            #    "Method": "External"
            # }
        ],
    }

    if settings.transcode_hi10p:
        profile["CodecProfiles"].append(
            {
                "Type": "Video",
                "Conditions": [
                    {
                        "Condition": "LessThanEqual",
                        "Property": "VideoBitDepth",
                        "Value": "8",
                    }
                ],
            }
        )

    if settings.transcode_dolby_vision:
        profile["CodecProfiles"].append(
            {
                "Type": "Video",
                "Conditions": [
                    {
                        "Condition": "NotEquals",
                        "Property": "VideoRangeType",
                        "Value": "DOVI",
                    }
                ],
            }
        )

    if settings.transcode_hdr:
        profile["CodecProfiles"].append(
            {
                "Type": "Video",
                "Conditions": [
                    {
                        "Condition": "Equals",
                        "Property": "VideoRangeType",
                        "Value": "SDR",
                    }
                ],
            }
        )

    if settings.transcode_hevc:
        profile["CodecProfiles"].append(
            {
                "Type": "Video",
                "Codec": "hevc",
                "Conditions": [
                    {
                        "Condition": "Equals",
                        "Property": "Width",
                        "Value": "0",
                    }
                ],
            }
        )
        profile["CodecProfiles"].append(
            {
                "Type": "Video",
                "Codec": "h265",
                "Conditions": [
                    {
                        "Condition": "Equals",
                        "Property": "Width",
                        "Value": "0",
                    }
                ],
            }
        )

    if settings.transcode_av1:
        profile["CodecProfiles"].append(
            {
                "Type": "Video",
                "Codec": "av1",
                "Conditions": [
                    {
                        "Condition": "Equals",
                        "Property": "Width",
                        "Value": "0",
                    }
                ],
            }
        )

    if settings.transcode_4k:
        profile["CodecProfiles"].append(
            {
                "Type": "Video",
                "Conditions": [
                    {
                        "Condition": "LessThanEqual",
                        "Property": "Width",
                        "Value": "1920",
                    },
                    {
                        "Condition": "LessThanEqual",
                        "Property": "Height",
                        "Value": "1080",
                    },
                ],
            }
        )

    if settings.always_transcode or force_transcode:
        profile["DirectPlayProfiles"] = []

    if is_tv:
        profile["TranscodingProfiles"].insert(
            0,
            {
                "Container": "ts",
                "Type": "Video",
                "AudioCodec": "mp3,aac",
                "VideoCodec": "h264",
                "Context": "Streaming",
                "Protocol": "hls",
                "MaxAudioChannels": "2",
                "MinSegments": "1",
                "BreakOnNonKeyFrames": True,
            },
        )

    return profile


def get_sub_display_title(stream: dict):
    return "{0}{1} ({2})".format(
        stream.get("Language", _("Unkn")).capitalize(),
        _(" Forced") if stream.get("IsForced") else "",
        stream.get("Codec"),
    )


def get_seq():
    global seq_num
    seq_num_lock.acquire()
    current = seq_num
    seq_num += 1
    seq_num_lock.release()
    return current


def none_fallback(value, fallback):
    if value is None:
        return fallback
    return value


def get_resource(*path):
    # Detect if bundled via pyinstaller.
    # From: https://stackoverflow.com/questions/404744/
    if getattr(sys, "_MEIPASS", False):
        application_path = os.path.join(getattr(sys, "_MEIPASS"), "jellyfin_mpv_shim")
    else:
        application_path = os.path.dirname(os.path.abspath(__file__))

    # ! Test code for Mac
    if getattr(sys, "frozen", False) and platform.system() == "Darwin":
        application_path = os.path.join(os.path.dirname(sys.executable), "../Resources")

    return os.path.join(application_path, *path)


def get_text(*path):
    with open(get_resource(*path)) as fh:
        return fh.read()
