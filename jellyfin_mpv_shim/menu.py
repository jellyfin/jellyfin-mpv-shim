from queue import Queue, LifoQueue
from .bulk_subtitle import process_series
from .conf import settings
from .utils import mpv_color_to_plex, get_sub_display_title
import time

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

COLOR_LIST = (
    ("White", "#FFFFFFFF"),
    ("Yellow", "#FFFFEE00"),
    ("Black", "#FF000000"),
    ("Cyan", "#FF00FFFF"),
    ("Blue", "#FF0000FF"),
    ("Green", "#FF00FF00"),
    ("Magenta", "#FFEE00EE"),
    ("Red", "#FFFF0000"),
    ("Gray", "#FF808080"),
)

SIZE_LIST = (
    ("Tiny", 50),
    ("Small", 75),
    ("Normal", 100),
    ("Large", 125),
    ("Huge", 200),
)

HEX_TO_COLOR = {v:c for c,v in COLOR_LIST}

class OSDMenu(object):
    def __init__(self, playerManager):
        self.playerManager = playerManager

        self.is_menu_shown = False
        self.menu_title = ""
        self.menu_stack = LifoQueue()
        self.menu_list = []
        self.menu_selection = 0
        self.menu_tmp = None

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

        self.playerManager._player.show_text(menu_text,2**30,1)

    def show_menu(self):
        self.is_menu_shown = True
        player = self.playerManager._player
        player.osd_back_color = '#CC333333'
        player.osd_font_size = 40

        if hasattr(player, 'osc'):
            player.osc = False
        
        self.menu_title = "Main Menu"
        self.menu_selection = 0

        if self.playerManager._video and not player.playback_abort:
            self.menu_list = [
                ("Change Audio", self.change_audio_menu),
                ("Change Subtitles", self.change_subtitle_menu),
                ("Change Video Quality", self.change_transcode_quality),
            ]
            if self.playerManager._video.parent.is_tv:
                self.menu_list.append(("Auto Set Audio/Subtitles (Entire Series)", self.change_tracks_menu))
            self.menu_list.append(("Quit and Mark Unwatched", self.unwatched_menu_handle))
        else:
            self.menu_list = []

        self.menu_list.extend([
            ("Preferences", self.preferences_menu),
            ("Close Menu", self.hide_menu)
        ])

        self.refresh_menu()

        # Wait until the menu renders to pause.
        time.sleep(0.2)

        if player.playback_abort:
            player.force_window = True
            player.keep_open = True
            player.play("")
            if settings.fullscreen:
                player.fs = True
        else:
            player.pause = True

    def hide_menu(self):
        player = self.playerManager._player
        if self.is_menu_shown:
            player.osd_back_color = '#00000000'
            player.osd_font_size = 55
            player.show_text("",0,0)
            player.force_window = False
            player.keep_open = False

            if hasattr(player, 'osc'):
                player.osc = settings.enable_osc

            if player.playback_abort:
                player.play("")
            else:
                player.pause = False

        self.is_menu_shown = False

    def put_menu(self, title, entries=None, selected=0):
        if entries is None:
            entries = []

        self.menu_stack.put((self.menu_title, self.menu_list, self.menu_selection))
        self.menu_title = title
        self.menu_list = entries
        self.menu_selection = selected

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
        self.playerManager.put_task(self.playerManager.set_streams, self.menu_list[self.menu_selection][2], None)
        self.playerManager.timeline_handle()
        self.menu_action("back")

    def change_audio_menu(self):
        self.put_menu("Select Audio Track")

        selected_aid = self.playerManager._video.aid
        audio_streams = [s for s in self.playerManager._video.media_source["MediaStreams"]
                         if s.get("Type") == "Audio"]
        for i, audio_track in enumerate(audio_streams):
            aid = audio_track.get("Index")
            self.menu_list.append([
                "{0} ({1})".format(audio_track.get("DisplayTitle"), audio_track.get("Title")),
                self.change_audio_menu_handle,
                aid
            ])
            if aid == selected_aid:
                self.menu_selection = i
    
    def change_subtitle_menu_handle(self):
        self.playerManager.put_task(self.playerManager.set_streams, None, self.menu_list[self.menu_selection][2])
        self.playerManager.timeline_handle()
        self.menu_action("back")

    def change_subtitle_menu(self):
        self.put_menu("Select Subtitle Track")

        selected_sid = self.playerManager._video.sid
        subtitle_streams = [s for s in self.playerManager._video.media_source["MediaStreams"]
                            if s.get("Type") == "Subtitle"]
        self.menu_list.append(["None", self.change_subtitle_menu_handle, -1])
        for i, subtitle_track in enumerate(subtitle_streams):
            sid = subtitle_track.get("Index")
            self.menu_list.append([
                "{0} ({1})".format(get_sub_display_title(subtitle_track), subtitle_track.get("Title")),
                self.change_subtitle_menu_handle,
                sid
            ])
            if sid == selected_sid:
                self.menu_selection = i+1

    def change_transcode_quality_handle(self):
        bitrate = self.menu_list[self.menu_selection][2]
        if bitrate == "none":
            self.playerManager._video.set_trs_override(None, False)
        elif bitrate == "max":
            self.playerManager._video.set_trs_override(None, True)
        else:
            self.playerManager._video.set_trs_override(bitrate, True)
        
        self.menu_action("back")
        self.playerManager.put_task(self.playerManager.restart_playback)
        self.playerManager.timeline_handle()

    def change_transcode_quality(self):
        handle = self.change_transcode_quality_handle
        self.put_menu("Select Transcode Quality", [
            ("No Transcode", handle, "none"),
            ("Maximum", handle, "max")
        ])

        for item in TRANSCODE_LEVELS:
            self.menu_list.append((item[0], handle, item[1]))

        self.menu_selection = 7
        cur_bitrate = self.playerManager._video.get_transcode_bitrate()
        for i, option in enumerate(self.menu_list):
            if cur_bitrate == option[2]:
                self.menu_selection = i

    def change_tracks_handle(self):
        mode = self.menu_list[self.menu_selection][2]
        process_series(mode, self.playerManager)

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
        process_series("manual", self.playerManager, aid, sid)

    def change_tracks_menu(self):
        self.put_menu("Select Audio/Subtitle for Series", [
            ("English Audio", self.change_tracks_handle, "dubbed"),
            ("Japanese Audio w/ English Subtitles", self.change_tracks_handle, "subbed"),
            ("Manual by Track Index (Less Reliable)", self.change_tracks_manual_s1),
        ])

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
        settings.remote_kbps = self.menu_list[self.menu_selection][2]
        settings.save()

        # Need to re-render preferences menu.
        for i in range(2):
            self.menu_action("back")
        self.preferences_menu()

    def transcode_settings_menu(self):
        self.put_menu("Select Default Transcode Profile")
        handle = self.transcode_settings_handle

        for i, item in enumerate(TRANSCODE_LEVELS):
            self.menu_list.append((item[0], handle, item[1]))
            if settings.remote_kbps == item[1]:
                self.menu_selection = i

    def get_subtitle_color(self, color):
        if color in HEX_TO_COLOR:
            return HEX_TO_COLOR[color]
        else:
            return mpv_color_to_plex(color)

    def sub_settings_handle(self):
        setting_name = self.menu_list[self.menu_selection][2]
        value = self.menu_list[self.menu_selection][3]
        setattr(settings, setting_name, value)
        settings.save()

        # Need to re-render preferences menu.
        for i in range(2):
            self.menu_action("back")
        self.preferences_menu()

        if self.playerManager._video.is_transcode:
            if setting_name == "subtitle_size":
                self.playerManager.put_task(self.playerManager.update_subtitle_visuals)
        else:
            self.playerManager.update_subtitle_visuals()

    def subtitle_color_menu(self):
        self.put_menu("Select Subtitle Color", [
            (name, self.sub_settings_handle, "subtitle_color", color)
            for name, color in COLOR_LIST
        ])

    def subtitle_size_menu(self):
        self.put_menu("Select Subtitle Size", [
            (name, self.sub_settings_handle, "subtitle_size", size)
            for name, size in SIZE_LIST
        ], selected=2)

    def subtitle_position_menu(self):
        self.put_menu("Select Subtitle Position", [
            ("Bottom", self.sub_settings_handle, "subtitle_position", "bottom"),
            ("Top", self.sub_settings_handle, "subtitle_position", "top"),
            ("Middle", self.sub_settings_handle, "subtitle_position", "middle"),
        ])

    def preferences_menu(self):
        self.put_menu("Preferences", [
            self.get_settings_toggle("Always Transcode", "always_transcode"),            
            self.get_settings_toggle("Auto Play", "auto_play"),
            ("Remote Transcode Quality: {0:0.1f} Mbps".format(settings.remote_kbps/1000), self.transcode_settings_menu),
            ("Subtitle Size: {0}".format(settings.subtitle_size), self.subtitle_size_menu),
            ("Subtitle Position: {0}".format(settings.subtitle_position), self.subtitle_position_menu),
            ("Subtitle Color: {0}".format(self.get_subtitle_color(settings.subtitle_color)), self.subtitle_color_menu),
            self.get_settings_toggle("Transcode H265", "transcode_h265"),
            self.get_settings_toggle("Transcode Hi10p", "transcode_hi10p"),
            self.get_settings_toggle("Direct Paths", "direct_paths"),
            self.get_settings_toggle("Auto Fullscreen", "fullscreen"),
            self.get_settings_toggle("Media Key Seek", "media_key_seek"),
            self.get_settings_toggle("Use Web Seek Pref", "use_web_seek"),
            self.get_settings_toggle("Display Mirroring", "display_mirroring"),
        ])

    def unwatched_menu_handle(self):
        self.playerManager.put_task(self.playerManager.unwatched_quit)
        self.playerManager.timeline_handle()
