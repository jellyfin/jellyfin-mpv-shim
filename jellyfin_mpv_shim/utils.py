import socket
import ipaddress

from .conf import settings
from datetime import datetime
from functools import wraps

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

def is_local_domain(domain):
    return ipaddress.ip_address(socket.gethostbyname(domain)).is_private

def mpv_color_to_plex(color):
    return '#'+color.lower()[3:]

def plex_color_to_mpv(color):
    return '#FF'+color.upper()[1:]
