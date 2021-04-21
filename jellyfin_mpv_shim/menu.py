from queue import LifoQueue
from .bulk_subtitle import process_series
from .conf import settings
from .utils import mpv_color_to_plex, get_sub_display_title
from .video_profile import VideoProfileManager
from .svp_integration import SVPManager
from .i18n import _

import time
import logging

log = logging.getLogger("menu")

TRANSCODE_LEVELS = (
    ("1080p 20 Mbps", 20000),
    ("1080p 12 Mbps", 12000),
    ("1080p 10 Mbps", 10000),
    ("720p 4 Mbps", 4000),
    ("720p 3 Mbps", 3000),
    ("720p 2.5 Mbps", 2500),
    ("540p 1.5 Mbps", 1500),
    ("540p 0.9 Mbps", 950),
    ("480p 0.4 Mbps", 400),
    ("320p 0.3 Mbps", 320),
)

COLOR_LIST = (
    (_("White"), "#FFFFFFFF"),
    (_("Yellow"), "#FFFFEE00"),
    (_("Black"), "#FF000000"),
    (_("Cyan"), "#FF00FFFF"),
    (_("Blue"), "#FF0000FF"),
    (_("Green"), "#FF00FF00"),
    (_("Magenta"), "#FFEE00EE"),
    (_("Red"), "#FFFF0000"),
    (_("Gray"), "#FF808080"),
)

SIZE_LIST = (
    (_("Tiny"), 50),
    (_("Small"), 75),
    (_("Normal"), 100),
    (_("Large"), 125),
    (_("Huge"), 200),
)

HEX_TO_COLOR = {v: c for c, v in COLOR_LIST}

lang_filter = set(settings.lang_filter.split(","))
if "und" in lang_filter:
    lang_filter.add(None)

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .player import playerManager as playerManager_type


class OSDMenu(object):
    def __init__(self, player_manager: "playerManager_type", player):
        self.playerManager = player_manager

        self.is_menu_shown = False
        self.menu_title = ""
        self.menu_stack = LifoQueue()
        self.menu_list = []
        self.menu_selection = 0
        self.menu_tmp = None
        self.mouse_back = False
        (
            self.original_osd_color,
            self.original_osd_size,
        ) = player_manager.get_osd_settings()

        self.profile_menu = None
        self.profile_manager = None
        if settings.shader_pack_enable:
            try:
                self.profile_manager = VideoProfileManager(self, player_manager, player)
                self.profile_menu = self.profile_manager.menu_action
            except Exception:
                log.error("Could not load profile manager.", exc_info=True)

        self.svp_menu = None
        try:
            self.svp_menu = SVPManager(self, player_manager)
        except Exception:
            log.error("Could not load SVP integration.", exc_info=True)

    # The menu is a bit of a hack...
    # It works using multiline OSD.
    # We also have to force the window to open.

    def refresh_menu(self):
        if not self.is_menu_shown:
            return

        items = self.menu_list
        selected_item = self.menu_selection

        if self.mouse_back:
            menu_text = "(<--) {0}".format(self.menu_title)
        else:
            menu_text = self.menu_title
        for i, item in enumerate(items):
            fmt = "\n   {0}"
            if i == selected_item and not self.mouse_back:
                fmt = "\n   **{0}**"
            menu_text += fmt.format(item[0])

        self.playerManager.show_text(menu_text, 2 ** 30, 1)

    def mouse_select(self, idx: int):
        if idx < 0 or idx > len(self.menu_list):
            return
        if idx == 0:
            self.mouse_back = True
        else:
            self.mouse_back = False
            self.menu_selection = idx - 1
        self.refresh_menu()

    def show_menu(self):
        self.is_menu_shown = True
        self.playerManager.set_osd_settings("#CC333333", 40)

        self.playerManager.enable_osc(False)
        self.playerManager.triggered_menu(True)

        self.menu_title = _("Main Menu")
        self.menu_selection = 0
        self.mouse_back = False

        if self.playerManager.is_playing():
            self.menu_list = [
                (_("Change Audio"), self.change_audio_menu),
                (_("Change Subtitles"), self.change_subtitle_menu),
                (_("Change Video Quality"), self.change_transcode_quality),
                (_("SyncPlay"), self.playerManager.syncplay.menu_action),
            ]
            if self.playerManager.update_check.new_version is not None:
                self.menu_list.insert(
                    0,
                    (
                        _("MPV Shim v{0} Release Info/Download").format(
                            self.playerManager.update_check.new_version
                        ),
                        self.playerManager.update_check.open,
                    ),
                )
            if self.profile_menu is not None:
                self.menu_list.append(
                    (_("Change Video Playback Profile"), self.profile_menu)
                )
            if self.playerManager.get_video().parent.is_tv:
                self.menu_list.append(
                    (
                        _("Auto Set Audio/Subtitles (Entire Series)"),
                        self.change_tracks_menu,
                    )
                )
            self.menu_list.append(
                (_("Quit and Mark Unwatched"), self.unwatched_menu_handle)
            )
            if settings.screenshot_menu:
                self.menu_list.append((_("Screenshot"), self.screenshot))
        else:
            self.menu_list = []
            if self.profile_menu is not None:
                self.menu_list.append((_("Video Playback Profiles"), self.profile_menu))

        if self.svp_menu is not None and self.svp_menu.is_available():
            self.menu_list.append((_("SVP Settings"), self.svp_menu.menu_action))

        self.menu_list.extend(
            [
                (_("Video Preferences"), self.video_preferences_menu),
                (_("Player Preferences"), self.player_preferences_menu),
                (_("Close Menu"), self.hide_menu),
            ]
        )

        self.refresh_menu()

        # Wait until the menu renders to pause.
        time.sleep(0.2)

        if self.playerManager.playback_is_aborted():
            self.playerManager.force_window(True)
        else:
            if not self.playerManager.syncplay.is_enabled():
                self.playerManager.set_paused(True)

    def hide_menu(self):
        if self.is_menu_shown:
            self.playerManager.set_osd_settings(
                self.original_osd_color, self.original_osd_size
            )
            self.playerManager.show_text("", 0, 0)

            self.playerManager.enable_osc(settings.enable_osc)
            self.playerManager.triggered_menu(False)
            self.playerManager.force_window(False)

            if not self.playerManager.playback_is_aborted():
                if not self.playerManager.syncplay.is_enabled():
                    self.playerManager.set_paused(False)

        self.is_menu_shown = False

    def screenshot(self):
        self.playerManager.show_text("", 0, 0)
        time.sleep(0.5)
        self.playerManager.screenshot()
        self.hide_menu()

    def put_menu(self, title: str, entries: Optional[list] = None, selected: int = 0):
        if entries is None:
            entries = []

        self.menu_stack.put((self.menu_title, self.menu_list, self.menu_selection))
        self.menu_title = title
        self.menu_list = entries
        self.menu_selection = selected

    def menu_action(self, action: str):
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
                    (
                        self.menu_title,
                        self.menu_list,
                        self.menu_selection,
                    ) = self.menu_stack.get_nowait()
            elif action == "ok":
                if self.mouse_back:
                    self.menu_action("back")
                else:
                    self.menu_list[self.menu_selection][1]()
            elif action == "home":
                self.show_menu()
            self.mouse_back = False
            self.refresh_menu()

    def change_audio_menu_handle(self):
        self.playerManager.put_task(
            self.playerManager.set_streams, self.menu_list[self.menu_selection][2], None
        )
        self.playerManager.timeline_handle()
        self.menu_action("back")

    def change_audio_menu(self):
        self.put_menu(_("Select Audio Track"))

        selected_aid = self.playerManager.get_video().aid
        audio_streams = [
            s
            for s in self.playerManager.get_video().media_source["MediaStreams"]
            if s.get("Type") == "Audio"
        ]
        for i, audio_track in enumerate(audio_streams):
            aid = audio_track.get("Index")
            if (
                settings.lang_filter_audio
                and aid != selected_aid
                and audio_track.get("Language") not in lang_filter
            ):
                continue

            self.menu_list.append(
                [
                    "{0} ({1})".format(
                        audio_track.get("DisplayTitle"), audio_track.get("Title")
                    ),
                    self.change_audio_menu_handle,
                    aid,
                ]
            )
            if aid == selected_aid:
                self.menu_selection = i

    def change_subtitle_menu_handle(self):
        self.playerManager.put_task(
            self.playerManager.set_streams, None, self.menu_list[self.menu_selection][2]
        )
        self.playerManager.timeline_handle()
        self.menu_action("back")

    def change_subtitle_menu(self):
        self.put_menu(_("Select Subtitle Track"))

        selected_sid = self.playerManager.get_video().sid
        subtitle_streams = [
            s
            for s in self.playerManager.get_video().media_source["MediaStreams"]
            if s.get("Type") == "Subtitle"
        ]
        self.menu_list.append([_("None"), self.change_subtitle_menu_handle, -1])
        for i, subtitle_track in enumerate(subtitle_streams):
            sid = subtitle_track.get("Index")
            if (
                settings.lang_filter_sub
                and sid != selected_sid
                and subtitle_track.get("Language") not in lang_filter
            ):
                continue

            self.menu_list.append(
                [
                    "{0} ({1})".format(
                        get_sub_display_title(subtitle_track),
                        subtitle_track.get("Title"),
                    ),
                    self.change_subtitle_menu_handle,
                    sid,
                ]
            )
            if sid == selected_sid:
                self.menu_selection = i + 1

    def change_transcode_quality_handle(self):
        bitrate = self.menu_list[self.menu_selection][2]
        if bitrate == "none":
            self.playerManager.get_video().set_trs_override(None, False)
        elif bitrate == "max":
            self.playerManager.get_video().set_trs_override(None, True)
        else:
            self.playerManager.get_video().set_trs_override(bitrate, True)

        self.menu_action("back")
        self.playerManager.put_task(self.playerManager.restart_playback)
        self.playerManager.timeline_handle()

    def change_transcode_quality(self):
        handle = self.change_transcode_quality_handle
        self.put_menu(
            _("Select Transcode Quality"),
            [(_("No Transcode"), handle, "none"), (_("Maximum"), handle, "max")],
        )

        for item in TRANSCODE_LEVELS:
            self.menu_list.append((item[0], handle, item[1]))

        self.menu_selection = 7
        cur_bitrate = self.playerManager.get_video().get_transcode_bitrate()
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
        self.put_menu(
            _("Select Audio/Subtitle for Series"),
            [
                (_("English Audio"), self.change_tracks_handle, "dubbed"),
                (
                    _("Japanese Audio w/ English Subtitles"),
                    self.change_tracks_handle,
                    "subbed",
                ),
                (
                    _("Manual by Track Index (Less Reliable)"),
                    self.change_tracks_manual_s1,
                ),
            ],
        )

    def settings_toggle_bool(self):
        _x, _x, key, name = self.menu_list[self.menu_selection]
        setattr(settings, key, not getattr(settings, key))
        settings.save()
        self.menu_list[self.menu_selection] = self.get_settings_toggle(name, key)

    def get_settings_toggle(self, name: str, setting: str):
        return (
            "{0}: {1}".format(name, getattr(settings, setting)),
            self.settings_toggle_bool,
            setting,
            name,
        )

    def transcode_settings_handle(self):
        settings.remote_kbps = self.menu_list[self.menu_selection][2]
        settings.save()

        # Need to re-render preferences menu.
        for i in range(2):
            self.menu_action("back")
        self.video_preferences_menu()

    def transcode_settings_menu(self):
        self.put_menu(_("Select Default Transcode Profile"))
        handle = self.transcode_settings_handle
        self.menu_list.append((_("No Transcode") + " 2 Gbps", handle, 2147483))

        for i, item in enumerate(TRANSCODE_LEVELS):
            self.menu_list.append((item[0], handle, item[1]))
            if settings.remote_kbps == item[1]:
                self.menu_selection = i

    @staticmethod
    def get_subtitle_color(color: str):
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
        self.video_preferences_menu()

        if self.playerManager.get_video().is_transcode:
            if setting_name == "subtitle_size":
                self.playerManager.put_task(self.playerManager.update_subtitle_visuals)
        else:
            self.playerManager.update_subtitle_visuals()

    def subtitle_color_menu(self):
        self.put_menu(
            _("Select Subtitle Color"),
            [
                (name, self.sub_settings_handle, "subtitle_color", color)
                for name, color in COLOR_LIST
            ],
        )

    def subtitle_size_menu(self):
        self.put_menu(
            _("Select Subtitle Size"),
            [
                (name, self.sub_settings_handle, "subtitle_size", size)
                for name, size in SIZE_LIST
            ],
            selected=2,
        )

    def subtitle_position_menu(self):
        self.put_menu(
            _("Select Subtitle Position"),
            [
                (_("Bottom"), self.sub_settings_handle, "subtitle_position", "bottom"),
                (_("Top"), self.sub_settings_handle, "subtitle_position", "top"),
                (_("Middle"), self.sub_settings_handle, "subtitle_position", "middle"),
            ],
        )

    def video_preferences_menu(self):
        self.put_menu(
            _("Video Preferences"),
            [
                (
                    _("Remote Transcode Quality: {0:0.1f} Mbps").format(
                        settings.remote_kbps / 1000
                    ),
                    self.transcode_settings_menu,
                ),
                (
                    _("Subtitle Size: {0}").format(settings.subtitle_size),
                    self.subtitle_size_menu,
                ),
                (
                    _("Subtitle Position: {0}").format(settings.subtitle_position),
                    self.subtitle_position_menu,
                ),
                (
                    _("Subtitle Color: {0}").format(
                        self.get_subtitle_color(settings.subtitle_color)
                    ),
                    self.subtitle_color_menu,
                ),
                self.get_settings_toggle(_("Transcode H265 to H264"), "transcode_h265"),
                self.get_settings_toggle(
                    _("Transcode Hi10p to 8bit"), "transcode_hi10p"
                ),
                self.get_settings_toggle(_("Direct Paths"), "direct_paths"),
                self.get_settings_toggle(_("Transcode to H265"), "transcode_to_h265"),
                self.get_settings_toggle(_("Disable Direct Play"), "always_transcode"),
            ],
        )

    def player_preferences_menu(self):
        self.put_menu(
            _("Player Preferences"),
            [
                self.get_settings_toggle(_("Auto Play"), "auto_play"),
                self.get_settings_toggle(_("Auto Fullscreen"), "fullscreen"),
                self.get_settings_toggle(_("Media Key Seek"), "media_key_seek"),
                self.get_settings_toggle(_("Use Web Seek Pref"), "use_web_seek"),
                self.get_settings_toggle(_("Display Mirroring"), "display_mirroring"),
                self.get_settings_toggle(_("Write Logs to File"), "write_logs"),
                self.get_settings_toggle(_("Check for Updates"), "check_updates"),
                self.get_settings_toggle(
                    _("Discord Rich Presence"), "discord_presence"
                ),
            ],
        )

    def unwatched_menu_handle(self):
        self.playerManager.put_task(self.playerManager.unwatched_quit)
        self.playerManager.timeline_handle()
