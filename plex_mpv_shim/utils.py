import logging
import os
import urllib.request, urllib.parse, urllib.error
import socket
import ipaddress

from .conf import settings
from datetime import datetime
from functools import wraps

log = logging.getLogger("utils")
plex_eph_tokens = {}

class Timer(object):
    def __init__(self):
        self.restart()

    def restart(self):
        self.started = datetime.now()

    def elapsedMs(self):
        return  self.elapsed() * 1e3

    def elapsed(self):
        return (datetime.now()-self.started).total_seconds()

def synchronous(tlockname):
    """
    A decorator to place an instance based lock around a method.
    From: http://code.activestate.com/recipes/577105-synchronization-decorator-for-class-methods/
    """

    def _synched(func):
        @wraps(func)
        def _synchronizer(self,*args, **kwargs):
            tlock = self.__getattribute__( tlockname)
            tlock.acquire()
            try:
                return func(self, *args, **kwargs)
            finally:
                tlock.release()
        return _synchronizer
    return _synched

def upd_token(domain, token):
    plex_eph_tokens[domain] = token

def get_plex_url(url, data=None):
    if not data:
        data = {}

    parsed_url = urllib.parse.urlsplit(url)
    domain = parsed_url.hostname

    if parsed_url.scheme != "https" and not settings.allow_http:
        raise ValueError("HTTP is not enabled in the configuration.")

    if domain in plex_eph_tokens:
        data.update({
            "X-Plex-Token": plex_eph_tokens[domain]
        })
    else:
        log.error("get_plex_url No token for: %s" % domain)

    data.update({
        "X-Plex-Version":           "2.0",
        "X-Plex-Client-Identifier": settings.client_uuid,
        "X-Plex-Provides":          "player",
        "X-Plex-Device-Name":       settings.player_name,
        "X-Plex-Model":             "RaspberryPI",
        "X-Plex-Device":            "RaspberryPI",

        # Lies
        "X-Plex-Product":           "Plex Home Theater",
        "X-Plex-Platform":          "Plex Home Theater"
    })

    # Kinda ghetto...
    sep = "?"
    if sep in url:
        sep = "&"

    if data:
        url = "%s%s%s" % (url, sep, urllib.parse.urlencode(data))

    log.debug("get_plex_url Created URL: %s" % url)

    return url

def safe_urlopen(url, data=None):
    """
    Opens a url and returns True if an HTTP 200 code is returned,
    otherwise returns False.
    """
    if not data:
        data = {}

    url = get_plex_url(url, data)

    try:
        page = urllib.request.urlopen(url)
        if page.code == 200:
            return True
        log.error("Error opening URL '%s': page returned %d" % (url,
                                                                page.code))
    except Exception as e:
        log.error("Error opening URL '%s':  %s" % (url, e))

    return False

def is_local_domain(domain):
    return ipaddress.ip_address(socket.gethostbyname(domain)).is_private

resolutions = {
    "240p": (320, 240),
    "360p": (640, 360),
    "480p": (848, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2160p": (3840, 2160),
    "4320p": (7680, 4320),
}

def get_resolution(name):
    if name in resolutions:
        return resolutions[name]
    return (1280, 720)

