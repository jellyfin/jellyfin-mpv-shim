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


def get_mpv_config_paths():
    """
    Get list of mpv config file paths to check, in priority order.
    
    Priority (highest to lowest):
    1. jellyfin-mpv-shim/mpv.conf - Shim-specific config (allows different settings for shim)
    2. $MPV_HOME/mpv.conf - User explicitly set MPV_HOME environment variable
    3. $XDG_CONFIG_HOME/mpv/mpv.conf or ~/.config/mpv/mpv.conf - Standard user config
    4. Platform-specific defaults - Fallback location
    
    Returns:
        List of paths to check. Only includes paths that exist.
    
    Note: The function returns the first file that CONTAINS the requested key,
    not the first file that EXISTS. This allows fallthrough to lower priority
    configs if a higher priority config exists but doesn't have the key.
    """
    from . import conffile
    from .constants import APP_NAME
    import os
    
    paths = []
    
    # 1. Shim's own config directory (highest priority)
    try:
        shim_mpv_conf = conffile.get(APP_NAME, "mpv.conf", True)
        if os.path.exists(shim_mpv_conf):
            paths.append(shim_mpv_conf)
    except Exception:
        pass
    
    # 2. MPV_HOME environment variable (user explicitly set)
    mpv_home = os.environ.get("MPV_HOME")
    if mpv_home:
        mpv_home_conf = os.path.join(mpv_home, "mpv.conf")
        if os.path.exists(mpv_home_conf):
            paths.append(mpv_home_conf)
    
    # 3. XDG_CONFIG_HOME on Linux/Unix (standard behavior)
    if not sys.platform.startswith("win32"):
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config:
            xdg_mpv_conf = os.path.join(xdg_config, "mpv", "mpv.conf")
        else:
            xdg_mpv_conf = os.path.join(os.path.expanduser("~"), ".config", "mpv", "mpv.conf")
        
        if os.path.exists(xdg_mpv_conf):
            paths.append(xdg_mpv_conf)
    
    # 4. Platform-specific defaults (lowest priority)
    if sys.platform.startswith("darwin"):
        # macOS: ~/Library/Application Support/mpv/mpv.conf
        macos_conf = os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", "mpv", "mpv.conf"
        )
        if os.path.exists(macos_conf):
            paths.append(macos_conf)
    elif sys.platform.startswith("win32") or sys.platform.startswith("cygwin"):
        # Windows: %APPDATA%\mpv\mpv.conf
        appdata = os.environ.get("APPDATA")
        if appdata:
            win_conf = os.path.join(appdata, "mpv", "mpv.conf")
            if os.path.exists(win_conf):
                paths.append(win_conf)
    
    return paths


# Module-level cache for parsed config files
_mpv_config_cache = {}
_mpv_config_mtime = {}


def get_mpv_config_value(key: str) -> Optional[str]:
    """
    Read a configuration value from mpv.conf file with caching.
    
    Checks multiple mpv config locations in priority order (see get_mpv_config_paths()).
    Returns the value from the first file that contains the requested key.
    
    Config files are cached in memory and only re-parsed if the file modification
    time changes, significantly improving performance for repeated lookups.
    
    Args:
        key: Configuration key to look for (e.g., "alang", "slang")
    
    Returns:
        The value as a string, or None if not found in any config file.
    
    Priority order:
    1. jellyfin-mpv-shim/mpv.conf (shim-specific settings)
    2. $MPV_HOME/mpv.conf (if MPV_HOME is set)
    3. $XDG_CONFIG_HOME/mpv/mpv.conf or ~/.config/mpv/mpv.conf (standard location)
    4. Platform-specific defaults (~/Library/Application Support/mpv/mpv.conf on macOS,
       %APPDATA%/mpv/mpv.conf on Windows)
    
    Note: If a higher priority file exists but doesn't contain the key, the function
    continues searching lower priority files. Only returns None if the key is not
    found in ANY file.
    """
    paths_to_check = get_mpv_config_paths()
    
    # Try each path in order
    for mpv_conf_path in paths_to_check:
        try:
            # Check if we need to reload the config file
            current_mtime = os.path.getmtime(mpv_conf_path)
            
            # Cache miss or file modified - parse the config
            if (mpv_conf_path not in _mpv_config_cache or 
                _mpv_config_mtime.get(mpv_conf_path) != current_mtime):
                
                config_dict = {}
                with open(mpv_conf_path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        # Strip whitespace and skip comments/empty lines
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        
                        # Parse key=value or key value format
                        if "=" in line:
                            conf_key, conf_value = line.split("=", 1)
                            conf_key = conf_key.strip()
                            conf_value = conf_value.strip()
                        else:
                            parts = line.split(None, 1)
                            if len(parts) != 2:
                                continue
                            conf_key, conf_value = parts
                        
                        # Store in dictionary (first occurrence wins)
                        if conf_key not in config_dict:
                            config_dict[conf_key] = conf_value
                
                # Update cache
                _mpv_config_cache[mpv_conf_path] = config_dict
                _mpv_config_mtime[mpv_conf_path] = current_mtime
                
                if settings.log_decisions:
                    log.debug(f"Parsed and cached mpv config from {mpv_conf_path}")
            
            # O(1) lookup from cache
            if key in _mpv_config_cache[mpv_conf_path]:
                value = _mpv_config_cache[mpv_conf_path][key]
                if settings.log_decisions:
                    log.info(f"Found {key}={value} in {mpv_conf_path}")
                return value
                
        except FileNotFoundError:
            # File was deleted between get_mpv_config_paths() and now
            continue
        except Exception:
            log.warning(f"Could not read {mpv_conf_path} for key '{key}'", exc_info=True)
            continue
    
    return None


def parse_language_list(lang_string: Optional[str]) -> list:
    """
    Parse a comma-separated language preference string into a list.
    Returns empty list if input is None or empty.
    """
    if not lang_string:
        return []
    
    # Split by comma and strip whitespace
    langs = [lang.strip() for lang in lang_string.split(",")]
    # Filter out empty strings
    return [lang for lang in langs if lang]
