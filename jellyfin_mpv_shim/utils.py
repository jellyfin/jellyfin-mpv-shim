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
    if ipaddress.ip_address(ip).is_private:
        return True

    if addr_info[0] == socket.AddressFamily.AF_INET:
        # IPv4 hairpin NAT: compare against our own WAN IP. We don't trust the
        # server's view of the client because reverse proxies routinely lie
        # (every connection looks like 127.0.0.1 / the proxy's IP).
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

    # IPv6: home networks typically use globally-routable prefixes, so the
    # is_private check above misses real LAN connections. Fall back to the
    # server's /System/Endpoint, which compares against the admin-configured
    # local network subnets. Less robust than the IPv4 path (a misconfigured
    # reverse proxy could lie) but the only signal available without enumerating
    # local interface addresses.
    try:
        endpoint = client.jellyfin.get_endpoint_info()
        return bool(endpoint.get("IsInNetwork"))
    except Exception:
        log.warning(
            "Could not query /System/Endpoint for IPv6 locality. Assuming remote.",
            exc_info=True,
        )
        return False


#: Ports a URL need not spell out. `https://h` and `https://h:443` are one
#: origin to a browser and to Jellyfin, and comparing raw ports made them two
#: -- so a sidecar on our own server, written with its port, was classified as
#: somebody else's and then got neither the header nor a token in its url.
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _origin(url):
    """``(scheme, host, port)`` with the default port filled in, or None."""
    parts = urllib.parse.urlparse(url)
    if not parts.hostname:
        return None
    return (parts.scheme, parts.hostname,
            parts.port or _DEFAULT_PORTS.get(parts.scheme))


def same_origin(url, server):
    """Whether ``url`` is on the same host as ``server``.

    Scheme and port count: an http URL is not the same origin as the https
    server we authenticated to, and sending a bearer token over the first
    would hand it to anyone on the path.

    One implementation, used by both the sync downloader and the player's
    mpv header. It lived in sync/manager.py, where its own comment predicted
    the player's defect: "'true by construction today' is exactly what stops
    being true when someone threads a server-supplied path through one of
    these."
    """
    try:
        mine, theirs = _origin(server), _origin(url)
    except Exception:
        # Includes the malformed port: `urlparse` is lazy, so `.port` raises
        # here and not at the parse. Answering False is failing closed --
        # every caller treats "not ours" as "send it no credential".
        return False
    return mine is not None and theirs == mine


def mpv_color_to_plex(color: str):
    return "#" + color.lower()[3:]


def plex_color_to_mpv(color: str):
    return "#FF" + color.upper()[1:]


def get_profile(
    is_remote: bool = False,
    video_bitrate: Optional[int] = None,
    force_transcode: bool = False,
):
    if video_bitrate is None:
        if is_remote:
            video_bitrate = settings.remote_kbps
        else:
            video_bitrate = settings.local_kbps

    if settings.force_video_codec:
        transcode_codecs = settings.force_video_codec
    elif (
        settings.allow_transcode_to_h265
        and not settings.prefer_transcode_to_h265
        and not settings.transcode_hevc
    ):
        transcode_codecs = "h264,h265,hevc,mpeg4,mpeg2video"
    elif (
        settings.allow_transcode_to_h265
        and settings.prefer_transcode_to_h265
        and not settings.transcode_hevc
    ):
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
                # 8, not 6. This cannot force a transcode, but it caps the
                # audio of one that is already happening, so 6 (inherited
                # from Kodi's profile) quietly cost 7.1 audio on every video
                # transcode -- docs/jellyfin-api-notes.md section 11.
                # Downmixing is mpv's job here, not the server's; see
                # audio_mode in conf.py.
                "MaxAudioChannels": "8",
            },
            {"Container": "jpeg", "Type": "Photo"},
        ],
        # No Container / AudioCodec on these entries ON PURPOSE: an empty
        # container in a DirectPlayProfile means "any" to the server, so this
        # declares that mpv direct-plays everything, which being
        # ffmpeg-backed it effectively does. Do NOT "tighten" this into an
        # explicit list -- anything left off (DSD, APE, WavPack, TAK, tracker
        # modules, whatever this build supports) silently transcodes to mp3
        # instead. See docs/jellyfin-api-notes.md section 11, which also has
        # the /Audio/universal comparison and what the round trip costs.
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
            {"Format": "dvdsub", "Method": "Embed"},
            {"Format": "dvbsub", "Method": "Embed"},
            {"Format": "pgs", "Method": "Embed"},
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

    return profile


def get_sub_display_title(stream: dict):
    """What to call a subtitle track in a picker.

    **The server's own ``DisplayTitle`` first.** That is the string
    jellyfin-web shows, and it is the only one carrying the *author's* label
    for the track -- which is the whole distinction between "Signs & Songs"
    and "Full" on a release that ships both. Built from Language / Forced /
    Codec instead, those two tracks come out as the same text and the picker
    offers a choice it cannot express (**[iw]**).

    The constructed form stays as the fallback, for a stream that has no
    DisplayTitle: an offline item rebuilt from the local catalog, or an
    older server. It also keeps ``Forced`` out of the composed label when
    DisplayTitle is used, since the server has already put it there.
    """
    display = (stream.get("DisplayTitle") or "").strip()
    if display:
        return display
    return "{0}{1} ({2})".format(
        (stream.get("Language") or _("Unkn")).capitalize(),
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


# -- system memory ---------------------------------------------------------
#
# Read rather than depended on: psutil would answer all of this in one line,
# and it is a compiled dependency to ask "how much RAM is there".

#: A machine with less RAM than this is small, whatever it is doing.
#:
#: Deliberately above a round 8 GiB, because the two sources disagree about
#: what an 8 GB machine has. Linux reports MemTotal, which is installed RAM
#: LESS the kernel and firmware reservation -- a nominal 8 GB box says about
#: 7.7 GiB -- while sysconf (the macOS path) reports exactly 8589934592. A
#: threshold at 8 GiB would therefore call the same hardware small on one
#: platform and roomy on the other, which is the worst of both. 8 GB machines
#: are the ones this is for, so the line goes above all of their spellings.
SMALL_MEMORY_BYTES = 9 * 1024 * 1024 * 1024
#: ...and any machine with less than this actually free right now is under
#: pressure, however much it started with.
TIGHT_MEMORY_BYTES = 2 * 1024 * 1024 * 1024

#: No machine running a video player has less RAM than this, so an answer
#: below it is not a small machine, it is a broken measurement.
_ABSURDLY_SMALL = 64 * 1024 * 1024

_total_memory = None            # never changes; read once


def _meminfo():
    """Linux: (MemTotal, MemAvailable) in bytes, or (None, None).

    MemAvailable rather than MemFree, and it is not a detail: MemFree
    excludes reclaimable page cache, so a healthy Linux box that has simply
    read some files looks nearly out of memory. MemAvailable is the kernel's
    own estimate of what a new allocation could actually get.
    """
    total = avail = None
    try:
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                name, _sep, rest = line.partition(":")
                if name == "MemTotal":
                    total = int(rest.split()[0]) * 1024
                elif name == "MemAvailable":
                    avail = int(rest.split()[0]) * 1024
                if total is not None and avail is not None:
                    break
    except (OSError, ValueError, IndexError):
        return None, None
    return total, avail


def _win_memory():
    """Windows: (total, available) in bytes via GlobalMemoryStatusEx."""
    import ctypes

    class _Status(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = _Status()
    status.dwLength = ctypes.sizeof(_Status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None, None
    return status.ullTotalPhys, status.ullAvailPhys


def system_memory():
    """``(total_bytes, available_bytes)``, either of which may be None.

    None means "could not tell", and every caller must read it as "assume
    there is room": a probe that cannot answer has no business degrading the
    app on a machine that may be perfectly comfortable.

    Total is answerable almost everywhere (POSIX ``sysconf`` covers macOS and
    the BSDs); *available* is the one that needs a per-platform source, and
    there is no portable one. macOS would need ``vm_stat``'s page breakdown,
    which is a subprocess per call, so it answers None and falls back to the
    total alone.
    """
    global _total_memory

    total = avail = None
    if sys.platform.startswith("linux"):
        total, avail = _meminfo()
    elif sys.platform.startswith("win"):
        try:
            total, avail = _win_memory()
        except Exception:
            log.debug("GlobalMemoryStatusEx failed", exc_info=True)
    if total is None:
        if _total_memory is None:
            try:
                _total_memory = (os.sysconf("SC_PHYS_PAGES")
                                 * os.sysconf("SC_PAGE_SIZE"))
            except (ValueError, OSError, AttributeError):
                _total_memory = False       # asked and answered: no
            if not _total_memory or _total_memory < _ABSURDLY_SMALL:
                # sysconf returns -1 for "indeterminate" WITHOUT raising
                # (CPython only raises when errno was set). -1 * the page
                # size is a negative "total" that is truthy, survives the
                # `or None` below, and compares less than every threshold --
                # pinning memory_is_tight() True for the life of the process
                # on a machine that may have 64 GiB. A floor rather than a
                # sign test, because both values can come back -1 and their
                # product is then a perfectly positive 1.
                _total_memory = False
        total = _total_memory or None
    return total, avail


def memory_is_tight(total=None, available=None):
    """Whether this machine is one to trade speed for memory on.

    True when the machine is small (under SMALL_MEMORY_BYTES of RAM at all)
    or busy (under TIGHT_MEMORY_BYTES free right now). Unknown is False --
    see system_memory.

    The two are separate questions on purpose. A 4 GiB box with 3 GiB free
    is not under pressure this second, but it has no headroom to be wrong
    about; a 64 GiB workstation with 1 GiB free is not small, but something
    else needs the room now.
    """
    if total is None and available is None:
        total, available = system_memory()
    if total is not None and total < SMALL_MEMORY_BYTES:
        return True
    return available is not None and available < TIGHT_MEMORY_BYTES
