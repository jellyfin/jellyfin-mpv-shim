import logging
import os
import urllib.request, urllib.parse, urllib.error

from conf import settings
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

def get_plex_url(url, data={}):
    domain = urllib.parse.urlsplit(url).hostname
    if domain in plex_eph_tokens:
        data.update({
            "X-Plex-Token": plex_eph_tokens[domain]
        })
    elif settings.myplex_token:
        data.update({
            "X-Plex-Token": settings.myplex_token
        })

    data.update({
        "X-Plex-Version":           "1.0",
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

def safe_urlopen(url, data={}):
    """
    Opens a url and returns True if an HTTP 200 code is returned,
    otherwise returns False.
    """
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


def find_exe(filename, search_path=None):
    """
    Given a search path, find executable.
    Originally fromL http://code.activestate.com/recipes/52224/
    """
    file_found = 0
    if search_path is None:
        search_path = os.environ.get("PATH", "")

    paths = search_path.split(os.pathsep)
    for path in paths:
        if os.access(os.path.join(path, filename), os.X_OK):
            file_found = 1
            break
    if file_found:
        return os.path.abspath(os.path.join(path, filename))
    else:
        return None
