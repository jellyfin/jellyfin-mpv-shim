import logging
import mpv
import os
import requests
import urllib.parse

from threading import RLock
from queue import Queue

from . import conffile
from .utils import synchronous, Timer
from .conf import settings
from .menu import OSDMenu

APP_NAME = 'plex-mpv-shim'

log = logging.getLogger('player')

# Q: What is with the put_task call?
# A: If something that modifies the url is called from a keybind
#    directly, it crashes the input handling. If you know why,
#    please tell me. I'd love to get rid of it.

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
        self._player = mpv.MPV(input_default_bindings=True, input_vo_keyboard=True, include=mpv_config)
        self.timeline_trigger = None
        self.action_trigger = None
        self.external_subtitles = {}
        self.external_subtitles_rev = {}
        self.menu = OSDMenu(self)

        if hasattr(self._player, 'osc'):
            self._player.osc = True
        else:
            log.warning("This mpv version doesn't support on-screen controller.")

        self.url = None
        self.evt_queue = Queue()

        @self._player.on_key_press('q')
        def handle_stop():
            self.stop()
            self.timeline_handle()

        @self._player.on_key_press('<')
        def handle_prev():
            self.put_task(self.play_prev)

        @self._player.on_key_press('>')
        def handle_next():
            self.put_task(self.play_next)

        @self._player.on_key_press('w')
        def handle_watched():
            self.put_task(self.watched_skip)

        @self._player.on_key_press('u')
        def handle_unwatched():
            self.put_task(self.unwatched_quit)

        @self._player.on_key_press('c')
        def menu_open():
            if not self.menu.is_menu_shown:
                self.menu.show_menu()
            else:
                self.menu.hide_menu()
        
        @self._player.on_key_press('esc')
        def menu_back():
            self.menu.menu_action('back')

        @self._player.on_key_press('enter')
        def menu_ok():
            self.menu.menu_action('ok')
        
        @self._player.on_key_press('left')
        def menu_left():
            if self.menu.is_menu_shown:
                self.menu.menu_action('left')
            else:
                self._player.command("seek", -5)
        
        @self._player.on_key_press('right')
        def menu_right():
            if self.menu.is_menu_shown:
                self.menu.menu_action('right')
            else:
                self._player.command("seek", 5)

        @self._player.on_key_press('up')
        def menu_up():
            if self.menu.is_menu_shown:
                self.menu.menu_action('up')
            else:
                self._player.command("seek", 60)

        @self._player.on_key_press('down')
        def menu_down():
            if self.menu.is_menu_shown:
                self.menu.menu_action('down')
            else:
                self._player.command("seek", -60)

        @self._player.on_key_press('space')
        def handle_pause():
            if self.menu.is_menu_shown:
                self.menu.menu_action('ok')
            else:    
                self.toggle_pause()

        # This gives you an interactive python debugger prompt.
        @self._player.on_key_press('~')
        def handle_debug():
            import pdb
            pdb.set_trace()

        @self._player.event_callback('idle')
        def handle_end(event):
            if self._video:
                self.put_task(self.finished_callback)

        self._video       = None
        self._lock        = RLock()
        self.last_update = Timer()

        self.__part      = 1

    # Put a task to the event queue.
    # This ensures the task executes outside
    # of an event handler, which causes a crash.
    def put_task(self, func, *args):
        self.evt_queue.put([func, args])
        if self.action_trigger:
            self.action_trigger.set()

    # Trigger the timeline to update all
    # clients immediately.
    def timeline_handle(self):
        if self.timeline_trigger:
            self.timeline_trigger.set()

    @synchronous('_lock')
    def update(self):
        while not self.evt_queue.empty():
            func, args = self.evt_queue.get()
            func(*args)
        if self._video and not self._player.playback_abort:
            if not self.is_paused():
                self.last_update.restart()

    def play(self, video, offset=0):
        url = video.get_playback_url()
        if not url:        
            log.error("PlayerManager::play no URL found")
            return

        self._play_media(video, url, offset)

    @synchronous('_lock')
    def _play_media(self, video, url, offset=0):
        self.url = url
        self.menu.hide_menu()

        self._player.play(self.url)
        self._player.wait_for_property("duration")
        self._player.fs = True
        self.external_subtitles = {}
        self.external_subtitles_rev = {}
        self._video  = video

        if offset > 0:
            self._player.playback_time = offset

        if not video.is_transcode:
            audio_idx = video.get_audio_idx()
            if audio_idx is not None:
                log.debug("PlayerManager::play selecting audio stream index=%s" % audio_idx)
                self._player.audio = audio_idx

            sub_idx = video.get_subtitle_idx()
            xsub_id = video.get_external_sub_id()
            if sub_idx is not None:
                log.debug("PlayerManager::play selecting subtitle index=%s" % sub_idx)
                self._player.sub = sub_idx
            elif xsub_id is not None:
                log.debug("PlayerManager::play selecting external subtitle id=%s" % xsub_id)
                self.load_external_sub(xsub_id)
            else:
                self._player.sub = 'no'

        self._player.pause = False
        self.timeline_handle()

    def exec_stop_cmd(self):
        if settings.stop_cmd:
            os.system(settings.stop_cmd)

    @synchronous('_lock')
    def stop(self):
        if not self._video or self._player.playback_abort:
            self.exec_stop_cmd()
            return

        log.debug("PlayerManager::stop stopping playback of %s" % self._video)

        self._video  = None
        self._player.command("stop")
        self._player.pause = False
        self.timeline_handle()
        self.exec_stop_cmd()

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
        self.timeline_handle()

    @synchronous('_lock')
    def seek(self, offset):
        """
        Seek to ``offset`` seconds
        """
        if not self._player.playback_abort:
            self._player.playback_time = offset
        self.timeline_handle()

    @synchronous('_lock')
    def set_volume(self, pct):
        if not self._player.playback_abort:
            self._player.volume = pct
        self.timeline_handle()

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
       
        self._video.set_played()

        if self._video.is_multipart():
            log.debug("PlayerManager::finished_callback media is multi-part, checking for next part")
            # Try to select the next part
            next_part = self.__part+1
            if self._video.select_part(next_part):
                self.__part = next_part
                log.debug("PlayerManager::finished_callback starting next part")
                self.play(self._video)
        
        elif self._video.parent.has_next and settings.auto_play:
            log.debug("PlayerManager::finished_callback starting next episode")
            self.play(self._video.parent.get_next().get_video(0))

        else:
            if settings.media_ended_cmd:
                os.system(settings.media_ended_cmd)
            log.debug("PlayerManager::finished_callback reached end")

    @synchronous('_lock')
    def watched_skip(self):
        if not self._video:
            return

        self._video.set_played()
        self.play_next()

    @synchronous('_lock')
    def unwatched_quit(self):
        if not self._video:
            return

        self._video.set_played(False)
        self.stop()

    @synchronous('_lock')
    def play_next(self):
        if self._video.parent.has_next:
            self.play(self._video.parent.get_next().get_video(0))
            return True
        return False

    @synchronous('_lock')
    def skip_to(self, key):
        media = self._video.parent.get_from_key(key)
        if media:
            self.play(media.get_video(0))
            return True
        return False

    @synchronous('_lock')
    def play_prev(self):
        if self._video.parent.has_prev:
            self.play(self._video.parent.get_prev().get_video(0))
            return True
        return False

    @synchronous('_lock')
    def restart_playback(self):
        current_time = self._player.playback_time
        self.play(self._video, current_time)
        return True

    @synchronous('_lock')
    def get_video_attr(self, attr, default=None):
        if self._video:
            return self._video.get_video_attr(attr, default)
        return default

    @synchronous('_lock')
    def set_streams(self, audio_uid, sub_uid):
        if not self._video.is_transcode:
            if audio_uid is not None:
                log.debug("PlayerManager::play selecting audio stream index=%s" % audio_uid)
                self._player.audio = self._video.audio_seq[audio_uid]

            if sub_uid == '0':
                log.debug("PlayerManager::play selecting subtitle stream (none)")
                self._player.sub = 'no'
            elif sub_uid is not None:
                log.debug("PlayerManager::play selecting subtitle stream index=%s" % sub_uid)
                if sub_uid in self._video.subtitle_seq:
                    self._player.sub = self._video.subtitle_seq[sub_uid]
                else:
                    log.debug("PlayerManager::play selecting external subtitle id=%s" % sub_uid)
                    self.load_external_sub(sub_uid)

        self._video.set_streams(audio_uid, sub_uid)

        if self._video.is_transcode:
            self.restart_playback()
        self.timeline_handle()
    
    @synchronous('_lock')
    def load_external_sub(self, sub_id):
        if sub_id in self.external_subtitles:
            self._player.sub = self.external_subtitles[sub_id]
        else:
            try:
                self._player.sub_add(self._video.get_external_sub(sub_id))
                self.external_subtitles[sub_id] = self._player.sub
                self.external_subtitles_rev[self._player.sub] = sub_id
            except SystemError:
                log.debug("PlayerManager::could not load external subtitle")

    def get_track_ids(self):
        if self._video.is_transcode:
            return self._video.get_transcode_streams()
        else:
            aid, sid = None, None
            if self._player.sub != 'no':
                if self._player.sub in self.external_subtitles_rev:
                    sid = self.external_subtitles_rev.get(self._player.sub, '')
                else:
                    sid = self._video.subtitle_uid.get(self._player.sub, '')

            if self._player.audio != 'no':
                aid = self._video.audio_uid.get(self._player.audio, '')
            return aid, sid

playerManager = PlayerManager()
