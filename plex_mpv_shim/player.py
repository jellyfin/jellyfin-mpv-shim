import logging
import mpv
import os
import requests
import urllib.parse

from threading import RLock
from queue import Queue, LifoQueue

from . import conffile
from .utils import synchronous, Timer, get_plex_url
from .conf import settings
from .bulk_subtitle import process_series

APP_NAME = 'plex-mpv-shim'

TRANSCODE_LEVELS = (
    ("1080p 20 Mbps", 20000),
    ("1080p 12 Mbps", 12000),
    ("1080p 10 Mbps", 10000),
    ("720p 4 Mbps", 4000),
    ("720p 3 Mbps", 3000),
    ("720p 2 Mbps", 2000),
    ("480p 1.5 Mbps", 1500),
    ("328p 0.7 Mbps", 720),
    ("240p 0.3 Mbps", 320),
    ("160p 0.2 Mbps", 208),
)

log = logging.getLogger('player')

# Q: What is with the put_task and timeline_handle?
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
        self.is_menu_shown = False
        self.menu_title = ""
        self.menu_stack = LifoQueue()
        self.menu_list = []
        self.menu_selection = 0
        self.menu_tmp = None

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
            self.timeline_handle()

        @self._player.on_key_press('>')
        def handle_next():
            self.put_task(self.play_next)
            self.timeline_handle()

        @self._player.on_key_press('w')
        def handle_watched():
            self.put_task(self.watched_skip)
            self.timeline_handle()

        @self._player.on_key_press('u')
        def handle_unwatched():
            self.put_task(self.unwatched_quit)
            self.timeline_handle()

        @self._player.on_key_press('c')
        def menu_open():
            if not self.is_menu_shown:
                self.show_menu()
            else:
                self.hide_menu()
        
        @self._player.on_key_press('esc')
        def menu_back():
            self.menu_action('back')

        @self._player.on_key_press('enter')
        def menu_ok():
            self.menu_action('ok')
        
        @self._player.on_key_press('left')
        def menu_left():
            if self.is_menu_shown:
                self.menu_action('left')
            else:
                self._player.command("seek", -5)
        
        @self._player.on_key_press('right')
        def menu_right():
            if self.is_menu_shown:
                self.menu_action('right')
            else:
                self._player.command("seek", 5)

        @self._player.on_key_press('up')
        def menu_up():
            if self.is_menu_shown:
                self.menu_action('up')
            else:
                self._player.command("seek", 60)

        @self._player.on_key_press('down')
        def menu_down():
            if self.is_menu_shown:
                self.menu_action('down')
            else:
                self._player.command("seek", -60)

        @self._player.on_key_press('space')
        def handle_pause():
            if self.is_menu_shown:
                self.menu_action('ok')
            else:    
                self.toggle_pause()
                self.timeline_handle()

        # This gives you an interactive python debugger prompt.
        @self._player.on_key_press('~')
        def handle_debug():
            import pdb
            pdb.set_trace()

        @self._player.event_callback('idle')
        def handle_end(event):
            if self._video:
                self.put_task(self.finished_callback)
                self.timeline_handle()

        self._video       = None
        self._lock        = RLock()
        self.last_update = Timer()

        self.__part      = 1

    # The menu is a bit of a hack...
    # It works using multiline OSD.
    # We also have to force the window to open.

    def refresh_menu(self):
        if not self.is_menu_shown:
            return
        
        items = self.menu_list
        selected_item = self.menu_selection

        menu_text = "{0}".format(self.menu_title)
        for i, item in enumerate(items):
            fmt = "\n   {0}"
            if i == selected_item:
                fmt = "\n   **{0}**"
            menu_text += fmt.format(item[0])

        self._player.show_text(menu_text,2**30,1)

    def show_menu(self):
        self.is_menu_shown = True
        self._player.osd_back_color = '#CC333333'
        self._player.osd_font_size = 40

        if hasattr(self._player, 'osc'):
            self._player.osc = False

        if self._player.playback_abort:
            self._player.force_window = True
            self._player.keep_open = True
            self._player.play("")
            self._player.fs = True
        else:
            self._player.pause = True
        
        self.menu_title = "Main Menu"
        self.menu_selection = 0

        if self._video and not self._player.playback_abort:
            self.menu_list = [
                ("Change Audio", self.change_audio_menu),
                ("Change Subtitles", self.change_subtitle_menu),
                ("Change Video Quality", self.change_transcode_quality),
                ("Auto Set Audio/Subtitles (Entire Series)", self.change_tracks_menu),
                ("Quit and Mark Unwatched", self.unwatched_menu_handle),
            ]
        else:
            self.menu_list = []

        self.menu_list.extend([
            ("Preferences", self.preferences_menu),
            ("Close Menu", self.hide_menu)
        ])

        self.put_task(self.unhide_menu)
        self.refresh_menu()

    def hide_menu(self):
        if self.is_menu_shown:
            self._player.osd_back_color = '#00000000'
            self._player.osd_font_size = 55
            self._player.show_text("",0,0)
            self._player.force_window = False
            self._player.keep_open = False

            if hasattr(self._player, 'osc'):
                self._player.osc = True

            if self._player.playback_abort:
                self._player.play("")
            else:
                self._player.pause = False

        self.is_menu_shown = False

    def menu_action(self, action):
        if not self.is_menu_shown and action in ("home", "ok"):
            self.show_menu()
        else:
            if action == "up":
                self.menu_selection = (self.menu_selection - 1) % len(self.menu_list)
            elif action == "down":
                self.menu_selection = (self.menu_selection + 1) % len(self.menu_list)
            elif action == "back":
                if self.menu_stack.empty():
                    self.hide_menu()
                else:
                    self.menu_title, self.menu_list, self.menu_selection = self.menu_stack.get_nowait()
            elif action == "ok":
                self.menu_list[self.menu_selection][1]()
            elif action == "home":
                self.show_menu()
            self.refresh_menu()

    def change_audio_menu_handle(self):
        if self._video.is_transcode:
            self.put_task(self.set_streams, self.menu_list[self.menu_selection][2], None)
            self.timeline_handle()
        else:
            self.set_streams(self.menu_list[self.menu_selection][2], None)
        self.menu_action("back")

    def change_audio_menu(self):
        self.menu_stack.put((self.menu_title, self.menu_list, self.menu_selection))
        self.menu_title = "Select Audio Track"
        self.menu_list = []
        self.menu_selection = 0

        selected_aid, _ = self.get_track_ids()
        audio_streams = playerManager._video._part_node.findall("./Stream[@streamType='2']")
        for i, audio_track in enumerate(audio_streams):
            aid = audio_track.get("id")
            self.menu_list.append([
                "{0} ({1})".format(audio_track.get("displayTitle"), audio_track.get("title")),
                self.change_audio_menu_handle,
                aid
            ])
            if aid == selected_aid:
                self.menu_selection = i
    
    def change_subtitle_menu_handle(self):
        if self._video.is_transcode:
            self.put_task(self.set_streams, None, self.menu_list[self.menu_selection][2])
            self.timeline_handle()
        else:
            self.set_streams(None, self.menu_list[self.menu_selection][2])
        self.menu_action("back")

    def change_subtitle_menu(self):
        self.menu_stack.put((self.menu_title, self.menu_list, self.menu_selection))
        self.menu_title = "Select Subtitle Track"
        self.menu_list = []
        self.menu_selection = 0

        _, selected_sid = self.get_track_ids()
        subtitle_streams = playerManager._video._part_node.findall("./Stream[@streamType='3']")
        self.menu_list.append(["None", self.change_subtitle_menu_handle, "0"])
        for i, subtitle_track in enumerate(subtitle_streams):
            sid = subtitle_track.get("id")
            self.menu_list.append([
                "{0} ({1})".format(subtitle_track.get("displayTitle"), subtitle_track.get("title")),
                self.change_subtitle_menu_handle,
                sid
            ])
            if sid == selected_sid:
                self.menu_selection = i+1

    def change_transcode_quality_handle(self):
        bitrate = self.menu_list[self.menu_selection][2]
        if bitrate == "none":
            self._video.set_trs_override(None, False, False)
        elif bitrate == "max":
            self._video.set_trs_override(None, True, False)
        else:
            self._video.set_trs_override(bitrate, True, True)
        
        self.menu_action("back")
        self.put_task(self.restart_playback)
        self.timeline_handle()

    def change_transcode_quality(self):
        self.menu_stack.put((self.menu_title, self.menu_list, self.menu_selection))
        self.menu_title = "Select Transcode Quality"
        handle = self.change_transcode_quality_handle
        self.menu_list = [
            ("No Transcode", handle, "none"),
            ("Maximum", handle, "max")
        ]

        for item in TRANSCODE_LEVELS:
            self.menu_list.append((item[0], handle, item[1]))

        self.menu_selection = 7
        cur_bitrate = self._video.get_transcode_bitrate()
        for i, option in enumerate(self.menu_list):
            if cur_bitrate == option[2]:
                self.menu_selection = i

    def change_tracks_handle(self):
        mode = self.menu_list[self.menu_selection][2]
        parentSeriesKey = self._video.parent.tree.find("./").get("parentKey") + "/children"
        url = self._video.parent.get_path(parentSeriesKey)
        process_series(mode, url, self)

    def change_tracks_manual_s1(self):
        self.change_audio_menu()
        for item in self.menu_list:
            item[1] = self.change_tracks_manual_s2
    
    def change_tracks_manual_s2(self):
        self.menu_tmp = self.menu_selection
        self.change_subtitle_menu()
        for item in self.menu_list:
            item[1] = self.change_tracks_manual_s3
    
    def change_tracks_manual_s3(self):
        aid, sid = self.menu_tmp, self.menu_selection - 1
        # Pop 3 menu items.
        for i in range(3):
            self.menu_action("back")
        parentSeriesKey = self._video.parent.tree.find("./").get("parentKey") + "/children"
        url = self._video.parent.get_path(parentSeriesKey)
        process_series("manual", url, self, aid, sid)

    def change_tracks_menu(self):
        self.menu_stack.put((self.menu_title, self.menu_list, self.menu_selection))
        self.menu_title = "Select Audio/Subtitle for Series"
        self.menu_selection = 0
        self.menu_list = [
            ("English Audio", self.change_tracks_handle, "dubbed"),
            ("Japanese Audio w/ English Subtitles", self.change_tracks_handle, "subbed"),
            ("Manual by Track Index (Less Reliable)", self.change_tracks_manual_s1),
        ]

    def settings_toggle_bool(self):
        _, _, key, name = self.menu_list[self.menu_selection]
        setattr(settings, key, not getattr(settings, key))
        settings.save()
        self.menu_list[self.menu_selection] = self.get_settings_toggle(name, key)

    def get_settings_toggle(self, name, setting):
        return (
            "{0}: {1}".format(name, getattr(settings, setting)),
            self.settings_toggle_bool,
            setting,
            name
        )

    def transcode_settings_handle(self):
        settings.transcode_kbps = self.menu_list[self.menu_selection][2]
        settings.save()

        # Need to re-render preferences menu.
        for i in range(2):
            self.menu_action("back")
        self.preferences_menu()

    def transcode_settings_menu(self):
        self.menu_stack.put((self.menu_title, self.menu_list, self.menu_selection))
        self.menu_title = "Select Default Transcode Profile"
        self.menu_selection = 0
        self.menu_list = []
        handle = self.transcode_settings_handle

        for i, item in enumerate(TRANSCODE_LEVELS):
            self.menu_list.append((item[0], handle, item[1]))
            if settings.transcode_kbps == item[1]:
                self.menu_selection = i

    def preferences_menu(self):
        self.menu_stack.put((self.menu_title, self.menu_list, self.menu_selection))
        self.menu_title = "Preferences"
        self.menu_selection = 0
        self.menu_list = [
            self.get_settings_toggle("Adaptive Transcode", "adaptive_transcode"),
            self.get_settings_toggle("Always Transcode", "always_transcode"),
            self.get_settings_toggle("Auto Play", "auto_play"),
            ("Transcode Quality: {0:0.1f} Mbps".format(settings.transcode_kbps/1000), self.transcode_settings_menu)
        ]

    def put_task(self, func, *args):
        self.evt_queue.put([func, args])

    def timeline_handle(self):
        if self.timeline_trigger:
            self.timeline_trigger.set()

    def unwatched_menu_handle(self):
        self.put_task(self.unwatched_quit)
        self.timeline_handle()

    def unhide_menu(self):
        # Sometimes, mpv completely ignores the OSD text.
        # Setting this value usually causes it to appear...
        self._player.osd_align_x = 'left'

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
        self.hide_menu()

        self._player.play(self.url)
        self._player.wait_for_property("duration")
        self._player.fs = True

        if offset > 0:
            self._player.playback_time = offset

        if not video.is_transcode:
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

        self._player.pause = False
        self._video  = video

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
                self._player.sub = self._video.subtitle_seq[sub_uid]

        self._video.set_streams(audio_uid, sub_uid)

        if self._video.is_transcode:
            self.restart_playback()
    
    def get_track_ids(self):
        if self._video.is_transcode:
            return self._video.get_transcode_streams()
        else:
            aid, sid = None, None
            if self._player.sub != 'no':
                sid = self._video.subtitle_uid.get(self._player.sub, '')

            if self._player.audio != 'no':
                aid = self._video.audio_uid.get(self._player.audio, '')
            return aid, sid

playerManager = PlayerManager()

