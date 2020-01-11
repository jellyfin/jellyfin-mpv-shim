import logging
import os
import urllib.request, urllib.parse, urllib.error
import socket
import ipaddress
import uuid
import re

from .conf import settings
from datetime import datetime
from functools import wraps

PLEX_TOKEN_RE = re.compile("(token|X-Plex-Token)=[^&]*")

log = logging.getLogger("utils")
plex_eph_tokens = {}
plex_sessions = {}
plex_transcode_sessions = {}

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

def get_transcode_session(domain, create=True):
    if domain not in plex_transcode_sessions:
        if not create:
            return
        session = str(uuid.uuid4())
        plex_transcode_sessions[domain] = session
    return plex_transcode_sessions[domain]

def clear_transcode_session(domain):
    if domain in plex_transcode_sessions:
        del plex_transcode_sessions[domain]

def get_session(domain):
    if domain not in plex_sessions:
        session = str(uuid.uuid4())
        plex_sessions[domain] = session
    return plex_sessions[domain]

def get_plex_url(url, data=None, quiet=False):
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
        "X-Plex-Version":             "2.0",
        "X-Plex-Client-Identifier":   settings.client_uuid,
        "X-Plex-Provides":            "player",
        "X-Plex-Device-Name":         settings.player_name,
        "X-Plex-Model":               "RaspberryPI",
        "X-Plex-Device":              "RaspberryPI",
        "X-Plex-Session-Identifier":  get_session(domain),

        # Lies
        "X-Plex-Product":             "Plex MPV Shim",
        "X-Plex-Platform":            "Plex Home Theater",
        "X-Plex-Client-Profile-Name": settings.client_profile,
    })

    # Kinda ghetto...
    sep = "?"
    if sep in url:
        sep = "&"

    if data:
        url = "%s%s%s" % (url, sep, urllib.parse.urlencode(data))

    if not quiet:
        log.debug("get_plex_url Created URL: %s" % sanitize_msg(url))

    return url

def safe_urlopen(url, data=None, quiet=False):
    """
    Opens a url and returns True if an HTTP 200 code is returned,
    otherwise returns False.
    """
    if not data:
        data = {}

    url = get_plex_url(url, data, quiet)

    try:
        page = urllib.request.urlopen(url)
        if page.code == 200:
            return True
        log.error("Error opening URL '%s': page returned %d" % (sanitize_msg(url),
                                                                page.code))
    except Exception as e:
        log.error("Error opening URL '%s':  %s" % (sanitize_msg(url), e))

    return False

def is_local_domain(domain):
    return ipaddress.ip_address(socket.gethostbyname(domain)).is_private

def sanitize_msg(text):
    if settings.sanitize_output:
        return re.sub(PLEX_TOKEN_RE, "\\1=REDACTED", text)
    return text

def mpv_color_to_plex(color):
    return '#'+color.lower()[3:]

def plex_color_to_mpv(color):
    return '#FF'+color.upper()[1:]
