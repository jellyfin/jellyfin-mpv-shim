import logging
import math
import os
import re
import mpv
import conffile

from threading import Thread, RLock
from time import sleep

from conf import settings
from utils import synchronous, Timer

# Scrobble progress to Plex server at most every 5 seconds
SCROBBLE_INTERVAL = 5
APP_NAME = 'plex-mpv-shim'

# Mark the item as watch when it is at 95% 
COMPLETE_PERCENT  = 0.95

log = logging.getLogger('player')

class PlayerManager(object):
    """
    Manages the relationship between a ``Player`` instance and a ``Media``
    item.  This is designed to be used as a singleton via the ``playerManager``
    instance in this module.  All communication between a caller and either the
    current ``player`` or ``media`` instance should be done through this class
    for thread safety reasons as all methods that access the ``player`` or
    ``media`` are thread safe.
    """
    def __init__(self):
        mpv_config = conffile.get(APP_NAME,"mpv.conf", True)
        self._player = mpv.MPV(input_default_bindings=True, input_vo_keyboard=True, osc=True, include=mpv_config)
        self.url = None

        @self._player.on_key_press('q')
        def handle_stop():
            self._player.command("stop")
            self._player.pause = False
            self._video = None

        @self._player.property_observer('path')
        def handle_end(_name, value):
            if self.url != value:
                self.finished_callback()

        self._video       = None
        self._lock        = RLock()
        self.last_update = Timer()

        self.__part      = 1

    @synchronous('_lock')
    def update(self):
        if self._video and not self._player.playback_abort:
            if self.last_update.elapsed() > SCROBBLE_INTERVAL and not self.is_paused():
                if not self._video.played:
                    position = self._player.playback_time
                    duration = self._video.get_duration()
                    if float(position)/float(duration)  >= COMPLETE_PERCENT:
                        log.info("PlayerManager::update setting media as watched")
                        self._video.set_played()
                    else:
                        log.info("PlayerManager::update updating media position")
                        self._video.update_position(position)
                self.last_update.restart()

    @synchronous('_lock')
    def play(self, video, offset=0):
        self.url = video.get_playback_url()
        if not self.url:
            log.error("PlayerManager::play no URL found")
            return
        
        self._player.play(self.url)
        self._player.wait_for_property("duration")
        self._player.fs = True

        if offset > 0:
            self._player.playback_time = offset

        audio_idx = video.get_audio_idx()
        if audio_idx is not None:
            log.debug("PlayerManager::play selecting audio stream index=%s" % audio_idx)
            self._player.audio = audio_idx

        sub_idx = video.get_subtitle_idx()
        if sub_idx is not None:
            log.debug("PlayerManager::play selecting subtitle index=%s" % sub_idx)
            self._player.sub = sub_idx
        else:
            self._player.sub = 'no'

        self._video  = video

    @synchronous('_lock')
    def stop(self):
        if not self._video or self._player.playback_abort:
            return

        log.debug("PlayerManager::stop stopping playback of %s" % self._video)

        self._player.command("stop")
        self._video  = None

    @synchronous('_lock')
    def get_volume(self, percent=False):
        if self._player:
            if not percent:
                return self._player.volume / 100
            return self._player.volume

    @synchronous('_lock')
    def toggle_pause(self):
        if not self._player.playback_abort:
            self._player.pause = not self._player.pause

    @synchronous('_lock')
    def seek(self, offset):
        """
        Seek to ``offset`` seconds
        """
        if not self._player.playback_abort:
            self._player.playback_time = offset

    @synchronous('_lock')
    def set_volume(self, pct):
        if not self._player.playback_abort:
            self._player.volume = pct

    @synchronous('_lock')
    def get_state(self):
        if self._player.playback_abort:
            return "stopped"

        if self._player.pause:
            return "paused"

        return "playing"
    
    @synchronous('_lock')
    def is_paused(self):
        if not self._player.playback_abort:
            return self._player.pause
        return False

    @synchronous('_lock')
    def finished_callback(self):
        if not self._video:
            return
        
        if self._video.is_multipart():
            log.debug("PlayerManager::finished_callback media is multi-part, checking for next part")
            # Try to select the next part
            next_part = self.__part+1
            if self._video.select_part(next_part):
                self.__part = next_part
                log.debug("PlayerManager::finished_callback starting next part")
                self.play(self._video)
        
        elif self._video.parent.has_next:
            log.debug("PlayerManager::finished_callback starting next episode")
            self.play(self._video.parent.get_next().get_video(0))

        log.debug("PlayerManager::finished_callback reached end")

    @synchronous('_lock')
    def play_next(self):
        if self._video.parent.has_next:
            self.play(self._video.parent.get_next().get_video(0))
            return True
        return False

    @synchronous('_lock')
    def play_prev(self):
        if self._video.parent.has_prev:
            self.play(self._video.parent.get_prev().get_video(0))
            return True
        return False

    @synchronous('_lock')
    def get_video_attr(self, attr, default=None):
        if self._video:
            return self._video.get_video_attr(attr, default)
        return default

    @synchronous('_lock')
    def set_streams(self, audio_uid, sub_uid):
        if audio_uid is not None:
            log.debug("PlayerManager::play selecting audio stream index=%s" % audio_uid)
            self._player.audio = self._video.audio_seq[audio_uid]

        if sub_uid is not None:
            log.debug("PlayerManager::play selecting subtitle stream index=%s" % sub_uid)
            self._player.sub = self._video.subtitle_seq[sub_uid]
        elif sub_uid == '':
            log.debug("PlayerManager::play selecting subtitle stream (none)")
            self._player.sub = 'no'

playerManager = PlayerManager()

