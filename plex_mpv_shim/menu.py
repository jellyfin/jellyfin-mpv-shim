from queue import Queue, LifoQueue
from .bulk_subtitle import process_series
from .conf import settings

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

        if player.playback_abort:
            player.force_window = True
            player.keep_open = True
            player.play("")
            player.fs = True
        else:
            player.pause = True
        
        self.menu_title = "Main Menu"
        self.menu_selection = 0

        if self.playerManager._video and not player.playback_abort:
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

        self.playerManager.put_task(self.unhide_menu)
        self.refresh_menu()

    def hide_menu(self):
        player = self.playerManager._player
        if self.is_menu_shown:
            player.osd_back_color = '#00000000'
            player.osd_font_size = 55
            player.show_text("",0,0)
            player.force_window = False
            player.keep_open = False

            if hasattr(player, 'osc'):
                player.osc = True

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
        if self.playerManager._video.is_transcode:
            self.playerManager.put_task(self.playerManager.set_streams, self.menu_list[self.menu_selection][2], None)
            self.playerManager.timeline_handle()
        else:
            self.playerManager.set_streams(self.menu_list[self.menu_selection][2], None)
        self.menu_action("back")

    def change_audio_menu(self):
        self.put_menu("Select Audio Track")

        selected_aid, _ = self.playerManager.get_track_ids()
        audio_streams = self.playerManager._video._part_node.findall("./Stream[@streamType='2']")
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
        if self.playerManager._video.is_transcode:
            self.playerManager.put_task(self.playerManager.set_streams, None, self.menu_list[self.menu_selection][2])
            self.playerManager.timeline_handle()
        else:
            self.playerManager.set_streams(None, self.menu_list[self.menu_selection][2])
        self.menu_action("back")

    def change_subtitle_menu(self):
        self.put_menu("Select Subtitle Track")

        _, selected_sid = self.playerManager.get_track_ids()
        subtitle_streams = self.playerManager._video._part_node.findall("./Stream[@streamType='3']")
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
            self.playerManager._video.set_trs_override(None, False, False)
        elif bitrate == "max":
            self.playerManager._video.set_trs_override(None, True, False)
        else:
            self.playerManager._video.set_trs_override(bitrate, True, True)
        
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
        parentSeriesKey = self.playerManager._video.parent.tree.find("./").get("parentKey") + "/children"
        url = self.playerManager._video.parent.get_path(parentSeriesKey)
        process_series(mode, url, self.playerManager)

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
        parentSeriesKey = self.playerManager._video.parent.tree.find("./").get("parentKey") + "/children"
        url = self.playerManager._video.parent.get_path(parentSeriesKey)
        process_series("manual", url, self.playerManager, aid, sid)

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
        settings.transcode_kbps = self.menu_list[self.menu_selection][2]
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
            if settings.transcode_kbps == item[1]:
                self.menu_selection = i

    def preferences_menu(self):
        self.put_menu("Preferences", [
            self.get_settings_toggle("Adaptive Transcode", "adaptive_transcode"),
            self.get_settings_toggle("Always Transcode", "always_transcode"),
            self.get_settings_toggle("Auto Play", "auto_play"),
            ("Transcode Quality: {0:0.1f} Mbps".format(settings.transcode_kbps/1000), self.transcode_settings_menu)
        ])

    def unwatched_menu_handle(self):
        self.playerManager.put_task(self.playerManager.unwatched_quit)
        self.playerManager.timeline_handle()

    def unhide_menu(self):
        # Sometimes, mpv completely ignores the OSD text.
        # Setting this value usually causes it to appear...
        self.playerManager._player.osd_align_x = 'left'
