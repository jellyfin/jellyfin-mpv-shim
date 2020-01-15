import logging
import mpv
import os
import sys
import urllib.parse
import time

from threading import RLock
from queue import Queue

from . import conffile
from .utils import synchronous, Timer, none_fallback
from .conf import settings
from .menu import OSDMenu

APP_NAME = 'jellyfin-mpv-shim'

SUBTITLE_POS = {
    "top": 0,
    "bottom": 100,
    "middle": 80,
}

log = logging.getLogger('player')

win_utils = None
if sys.platform.startswith("win32") or sys.platform.startswith("cygwin"):
    try:
        from . import win_utils
    except ModuleNotFoundError:
        log.warning("win_utils is not available.")

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
        self.should_send_timeline = False
        self.start_time = None

        if hasattr(self._player, 'osc'):
            self._player.osc = True
        else:
            log.warning("This mpv version doesn't support on-screen controller.")

        self.url = None
        self.evt_queue = Queue()

        @self._player.on_key_press('CLOSE_WIN')
        @self._player.on_key_press('q')
        def handle_stop():
            self.stop()

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

        @self._player.property_observer('eof-reached')
        def handle_end(_name, reached_end):
            if self._video and reached_end:
                self.put_task(self.finished_callback)

        self._video       = None
        self._lock        = RLock()
        self._tl_lock        = RLock()
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
        self.should_send_timeline = False
        self.start_time = time.time()
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
        if settings.fullscreen:
            self._player.fs = True
        self._player.force_media_title = video.get_proper_title()
        self._video = video
        self.external_subtitles = {}
        self.external_subtitles_rev = {}

        self.upd_player_hide()
        self.configure_streams()
        self.update_subtitle_visuals()

        if win_utils:
            win_utils.raise_mpv()

        if offset is not None and offset > 0:
            self._player.playback_time = offset

        self.send_timeline_initial()
        self._player.pause = False
        self.should_send_timeline = True

    def exec_stop_cmd(self):
        if settings.stop_cmd:
            os.system(settings.stop_cmd)

    @synchronous('_lock')
    def stop(self):
        if not self._video or self._player.playback_abort:
            self.exec_stop_cmd()
            return

        log.debug("PlayerManager::stop stopping playback of %s" % self._video)

        self._video.terminate_transcode()
        self.send_timeline_stopped()
        self._video = None
        self._player.command("stop")
        self._player.pause = False
        self.exec_stop_cmd()

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
        if self._video.parent.has_next and settings.auto_play:
            log.debug("PlayerManager::finished_callback starting next episode")
            self.play(self._video.parent.get_next().video)
        else:
            if settings.media_ended_cmd:
                os.system(settings.media_ended_cmd)
            log.debug("PlayerManager::finished_callback reached end")
            self.send_timeline_stopped()

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

        video = self._video
        self.stop()
        video.set_played(False)

    @synchronous('_lock')
    def play_next(self):
        if self._video.parent.has_next:
            self.play(self._video.parent.get_next().video)
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
            self.play(self._video.parent.get_prev().video)
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
    def configure_streams(self):
        audio_uid = self._video.aid
        sub_uid = self._video.sid

        if audio_uid is not None and not self._video.is_transcode:
                log.debug("PlayerManager::play selecting audio stream index=%s" % audio_uid)
                self._player.audio = self._video.audio_seq[audio_uid]
        
        if sub_uid is None or sub_uid == -1:
            log.debug("PlayerManager::play selecting subtitle stream (none)")
            self._player.sub = 'no'
        else:
            log.debug("PlayerManager::play selecting subtitle stream index=%s" % sub_uid)
            if sub_uid in self._video.subtitle_seq:
                self._player.sub = self._video.subtitle_seq[sub_uid]
            elif sub_uid in self._video.subtitle_url:
                log.debug("PlayerManager::play selecting external subtitle id=%s" % sub_uid)
                self.load_external_sub(sub_uid)

    @synchronous('_lock')
    def set_streams(self, audio_uid, sub_uid):
        need_restart = self._video.set_streams(audio_uid, sub_uid)

        if need_restart:
            self.restart_playback()
        else:
            self.configure_streams()
        self.timeline_handle()
    
    @synchronous('_lock')
    def load_external_sub(self, sub_id):
        if sub_id in self.external_subtitles:
            self._player.sub = self.external_subtitles[sub_id]
        else:
            try:
                self._player.sub_add(self._video.subtitle_url[sub_id])
                self.external_subtitles[sub_id] = self._player.sub
                self.external_subtitles_rev[self._player.sub] = sub_id
            except SystemError:
                log.debug("PlayerManager::could not load external subtitle")

    @synchronous('_lock')
    def toggle_fullscreen(self):
        self._player.fs = not self._player.fs

    @synchronous('_lock')
    def set_mute(self, mute):
        self._player.mute = mute

    @synchronous('_lock')
    def screenshot(self):
        self._player.screenshot()

    def get_track_ids(self):
        return self._video.aid, self._video.sid

    def update_subtitle_visuals(self):
        self._player.sub_pos = SUBTITLE_POS[settings.subtitle_position]
        self._player.sub_scale = settings.subtitle_size / 100
        self._player.sub_color = settings.subtitle_color
        self.timeline_handle()
    
    def get_timeline_options(self):
        # PlaylistItemId is dynamicallt generated. A more stable Id will be used
        # if queue manipulation is added as a feature.
        player = self._player
        safe_pos = player.playback_time or 0
        options = {
            "VolumeLevel": int(player.volume),
            "IsMuted": player.mute,
            "IsPaused": player.pause,
            "RepeatMode": "RepeatNone",
            #"MaxStreamingBitrate": 140000000,
            "PositionTicks": int(safe_pos * 1000) * 10000,
            "PlaybackStartTimeTicks": int(self.start_time * 1000) * 10000,
            "SubtitleStreamIndex": none_fallback(self._video.sid, -1),
            "AudioStreamIndex": none_fallback(self._video.aid, -1),
            "BufferedRanges":[{
                "start": int(player.time_pos * 1000) * 10000,
                "end": int(((player.duration - safe_pos * none_fallback(player.cache_buffering_state, 0) / 100) + safe_pos) * 1000) * 10000
            }],
            "PlayMethod": "Transcode" if self._video.is_transcode else "DirectPlay",
            "PlaySessionId": self._video.playback_info["PlaySessionId"],
            "PlaylistItemId": self._video.parent.queue[self._video.parent.seq]["PlaylistItemId"],
            "MediaSourceId": self._video.media_source["Id"],
            "CanSeek": True,
            "ItemId": self._video.item_id,
            "NowPlayingQueue": self._video.parent.queue,
        }
        return options

    @synchronous('_tl_lock')
    def send_timeline(self):
        if self.should_send_timeline and self._video and not self._player.playback_abort:
            self._video.client.jellyfin.session_progress(self.get_timeline_options())

    @synchronous('_tl_lock')
    def send_timeline_initial(self):
        self._video.client.jellyfin.session_playing(self.get_timeline_options())
    
    @synchronous('_tl_lock')
    def send_timeline_stopped(self):
        self.should_send_timeline = False
        self._video.client.jellyfin.session_stop(self.get_timeline_options())

    def upd_player_hide(self):
        self._player.keep_open = self._video.parent.has_next

playerManager = PlayerManager()
