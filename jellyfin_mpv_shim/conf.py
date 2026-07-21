import logging
import os
import uuid
import socket
import json
import os.path
import sys
import getpass
import threading
from typing import List, Optional
from .settings_base import SettingsBase, object_types
from .language_config import LanguageRule, parse_language_config

# Register the structured-type parser. Done here (rather than in
# settings_base.py) to keep settings_base free of dependencies that would
# otherwise cycle through conf.py.
object_types[Optional[List[LanguageRule]]] = parse_language_config

log = logging.getLogger("conf")
config_path = None

# Bump when a default changes in a way existing installs must pick up, and add
# the corresponding step to Settings._migrate().
#   1: transcode_dolby_vision defaults off (mpv plays Dolby Vision natively).
CONFIG_VERSION = 1

# Serializes writers of the config file. save() is reachable from the UI
# action loop and from background workers (e.g. the download-folder move);
# unsynchronized truncate+rewrite writers can interleave into invalid JSON,
# which load() swallows — silently resetting every setting on next launch.
_save_lock = threading.Lock()


def get_default_sdir():
    if sys.platform.startswith("win32"):
        if os.environ.get("USERPROFILE"):
            return os.path.join(os.environ["USERPROFILE"], "Desktop")
        else:
            username = getpass.getuser()
            return os.path.join(r"C:\Users", username, "Desktop")
    else:
        return None


class Settings(SettingsBase):
    # Schema revision of the config on disk, used to apply one-time migrations
    # when a default changes in a way that should reach existing installs.
    # A config written before this key existed loads as 0. See _migrate().
    config_version: int = 0
    player_name: str = socket.gethostname()
    audio_output: str = "hdmi"
    client_uuid: str = str(uuid.uuid4())
    media_ended_cmd: Optional[str] = None
    pre_media_cmd: Optional[str] = None
    stop_cmd: Optional[str] = None
    auto_play: bool = True
    # Persisted playback volume (0-100), kept separately for music and video —
    # the loudness gap between the two means users want different levels.
    # Applied when a track starts and reconciled back on the timeline tick.
    music_volume: int = 100
    video_volume: int = 100
    idle_cmd: Optional[str] = None
    idle_ended_cmd: Optional[str] = None
    play_cmd: Optional[str] = None
    idle_cmd_delay: int = 60
    # Quit mpv after this many idle seconds to free the window / GPU / memory;
    # it is re-created on the next play (or when the library is reopened from
    # the tray). On by default: with the browser and the player sharing one
    # process, this is what makes a minimized app cost almost nothing.
    mpv_idle_quit: bool = True
    mpv_idle_quit_secs: int = 300
    direct_paths: bool = False
    remote_direct_paths: bool = False
    path_substitutions: list = []
    always_transcode: bool = False
    transcode_hi10p: bool = False
    transcode_hdr: bool = False
    transcode_hevc: bool = False
    transcode_av1: bool = False
    transcode_4k: bool = False
    # Off by default: mpv handles Dolby Vision natively now, so force-
    # transcoding it to SDR costs server CPU and loses the HDR presentation
    # for no benefit. CONFIG_VERSION 1 migrates existing configs off it.
    transcode_dolby_vision: bool = False
    allow_transcode_to_h265: bool = False
    prefer_transcode_to_h265: bool = False
    remote_kbps: int = 10000
    local_kbps: int = 2147483
    subtitle_size: int = 100
    subtitle_color: str = "#FFFFFFFF"
    subtitle_position: str = "bottom"
    # Off by default since the browser and the player share one window: a
    # cast target that only ever showed video could reasonably grab the
    # screen, but an app you are actively browsing should not go fullscreen
    # out from under you when you press play.
    fullscreen: bool = False
    enable_gui: bool = True
    # Run the in-window library browser fullscreen. Off by default: browsing is
    # a desktop activity, and `fullscreen` (which still applies to playback)
    # would otherwise make the browser take over the screen at startup.
    browser_fullscreen: bool = False
    start_minimized: bool = False
    # Window size for the player/browser window. mpv's own default is a fixed
    # 960x540 regardless of how big the display is, which is small for a
    # browsable UI. These are rewritten on exit when remember_window_size is
    # on, so they double as "the size you left it at".
    window_width: int = 1280
    window_height: int = 720
    window_maximized: bool = False
    # Persist the window size across launches. Off means window_width/height
    # are a fixed preference the app always opens at, which is what you want
    # if you deliberately pinned a size.
    remember_window_size: bool = True
    # When True, closing the library-browser window hides it to the system tray
    # (keeping the app alive as a cast target) rather than exiting. Defaults to
    # the historical behaviour; the user is prompted once on first close.
    close_to_tray: bool = True
    library_image_cache_mb: int = 256
    library_last_server: Optional[str] = None
    sync_path: Optional[str] = None
    work_offline: bool = False
    prefer_downloaded: bool = True
    # Auto-download: keep upcoming episodes on disk without being asked.
    # Off by default — it is the only feature that writes to the user's disk
    # unattended, so it is opt-in rather than something to discover after the
    # fact. It runs on a schedule and only while nothing is playing, so it
    # never competes with streaming for bandwidth.
    auto_download_enable: bool = False
    # Sources. next_up follows the server's Next Up across every series;
    # lookahead follows the series you are actually working through, queueing
    # this many episodes past the last one you watched (0 disables it).
    auto_download_next_up: bool = True
    auto_download_lookahead: int = 2
    # Budget for auto-downloads only (see SyncDB.auto_size). Downloads the
    # user asked for are never counted against it and never reaped.
    auto_download_max_gb: int = 20
    # Retention. Both can be on; delete_watched keeps the footprint near the
    # lookahead window, keep_days reclaims a show that was abandoned midway.
    # keep_days = 0 means never expire on age alone.
    auto_download_delete_watched: bool = True
    auto_download_keep_days: int = 30
    auto_download_interval_mins: int = 60
    media_key_seek: bool = False
    mpv_ext: bool = sys.platform.startswith("darwin")
    mpv_ext_path: Optional[str] = None
    mpv_ext_ipc: Optional[str] = None
    mpv_ext_start: bool = True
    mpv_ext_start_retries: int = 10
    mpv_ext_start_retry_delay_ms: int = 3000
    mpv_ext_no_ovr: bool = False
    enable_osc: bool = True
    use_web_seek: bool = False
    # Locked-down cast-target mode: the cast screen is the only page, and
    # the library cannot be reached from the machine itself. Replaces
    # display_mirroring, which was a *second* UI rather than a page and so
    # could not coexist with the browser. NOT a security boundary — anyone
    # who can attach input can usually also edit this file, and the tray
    # still reaches Settings. It stops a plugged-in mouse from playing the
    # library, which is what it is for.
    headless: bool = False
    log_decisions: bool = False
    mpv_log_level: str = "info"
    idle_when_paused: bool = False
    stop_idle: bool = False
    kb_stop: str = "q"
    kb_prev: str = "<"
    kb_next: str = ">"
    kb_watched: str = "w"
    kb_unwatched: str = "u"
    kb_menu: str = "c"
    kb_menu_esc: str = "esc"
    kb_menu_ok: str = "enter"
    kb_menu_left: str = "left"
    kb_menu_right: str = "right"
    kb_menu_up: str = "up"
    kb_menu_down: str = "down"
    kb_pause: str = "space"
    kb_fullscreen: str = "f"
    kb_debug: str = "~"
    kb_kill_shader: str = "k"
    seek_up: int = 60
    seek_down: int = -60
    seek_right: int = 5
    seek_left: int = -5
    seek_v_exact: bool = False
    seek_h_exact: bool = False
    shader_pack_enable: bool = True
    shader_pack_custom: bool = False
    shader_pack_remember: bool = True
    shader_pack_profile: Optional[str] = None
    shader_pack_subtype: str = "lq"
    svp_enable: bool = False
    svp_url: str = "http://127.0.0.1:9901/"
    svp_socket: Optional[str] = None
    sanitize_output: bool = True
    write_logs: bool = False
    playback_timeout: int = 30
    sync_max_delay_speed: int = 50
    sync_max_delay_skip: int = 300
    sync_method_thresh: int = 2000
    sync_speed_time: int = 1000
    sync_speed_attempts: int = 3
    sync_attempts: int = 5
    sync_revert_seek: bool = True
    sync_osd_message: bool = True
    screenshot_menu: bool = True
    check_updates: bool = True
    notify_updates: bool = True
    lang: Optional[str] = None
    discord_presence: bool = False
    ignore_ssl_cert: bool = False
    menu_mouse: bool = True
    media_keys: bool = True
    connect_retry_mins: int = 0
    transcode_warning: bool = True
    lang_filter: str = "und,eng,jpn,mis,mul,zxx"
    lang_filter_sub: bool = False
    lang_filter_audio: bool = False
    remember_audio_track: bool = True
    remember_subtitle_track: bool = True
    language_preference: str = "custom"
    preferred_language: str = "eng"
    force_set_played: bool = False
    screenshot_dir: Optional[str] = get_default_sdir()
    raise_mpv: bool = True
    force_video_codec: Optional[str] = None
    force_audio_codec: Optional[str] = None
    health_check_interval: Optional[int] = 300
    skip_intro_always: bool = False
    skip_intro_enable: bool = True
    skip_credits_always: bool = False
    skip_credits_enable: bool = True
    # Seeking forward during an intro/credits window skips the whole
    # segment. Applies to keyboard/remote seeks; seeks made from the
    # jellyfin OSC's seekbar never trigger it (it has its own button).
    skip_intro_on_seek: bool = True
    thumbnail_enable: bool = True
    thumbnail_osc_builtin: bool = True
    # In-player UI: "mpvtk" (the in-window playback HUD rendered by the
    # library browser — jellyfin-web styled, remote navigable; needs
    # enable_gui, falls back to "mpv" otherwise), "mpv" (stock
    # mpv OSC patched with trickplay previews), or "default" (whatever
    # OSC is built into the mpv binary / the user's own scripts).
    # "jellyfin" is a legacy alias for "mpvtk" (the lua OSC it used to
    # name was retired once the HUD reached parity).
    osc_style: str = "mpvtk"
    # Scale factor for the whole in-player UI (tiles, text, chrome).
    # null follows the display: mpv's display-hidpi-scale, which is 1.0
    # on X11 and the compositor's factor on Wayland/macOS. Set a number
    # (1.5, 2.0) to force it — useful on a 1x display to see what a HiDPI
    # user gets. Read once at startup; changing it needs a restart,
    # because rescaling live means dropping every cached bitmap and that
    # is only safe on the libmpv path once mpv is gone.
    ui_scale: Optional[float] = None
    # While a video plays with the HUD hidden, grab UP/DOWN/LEFT/RIGHT
    # (and ENTER) to summon/drive the HUD. Off by default: mpv's own
    # seek keys keep working and only hud_wake_key is taken over.
    hud_grab_keys: bool = False
    # The key that summons the HUD for keyboard driving while it is
    # hidden (mpv key name syntax). ENTER also toggles pause on wake.
    hud_wake_key: str = "ENTER"
    thumbnail_preferred_size: int = 320
    tls_client_cert: Optional[str] = None
    tls_client_key: Optional[str] = None
    tls_server_ca: Optional[str] = None
    language_config: Optional[List[LanguageRule]] = None

    def _migrate(self):
        """Apply one-time upgrades for configs written by older versions.

        Every key is written to disk on save (see SettingsBase.dict), so a
        changed default alone never reaches an existing install -- the old
        value is already recorded. Migrations here bring those installs
        forward. Returns whether anything changed (the caller saves).

        Only migrate a default whose old value is actively harmful; a setting
        the user may have deliberately chosen cannot be distinguished from an
        inherited default, so each step here silently overrides a real choice.
        """
        changed = False
        if self.config_version < 1:
            # mpv plays Dolby Vision natively now, so the old default of
            # force-transcoding it to SDR just burns server CPU and throws
            # away the HDR presentation.
            if self.transcode_dolby_vision:
                log.info(
                    "Config migration: disabling transcode_dolby_vision "
                    "(mpv now supports Dolby Vision natively)."
                )
                self.transcode_dolby_vision = False
        if self.config_version != CONFIG_VERSION:
            self.config_version = CONFIG_VERSION
            changed = True
        return changed

    def __get_file(self, path: str, mode: str = "r", create: bool = True):
        created = False

        if not os.path.exists(path):
            try:
                _fh = open(path, mode)
            except IOError as e:
                if e.errno == 2 and create:
                    fh = open(path, "w")
                    json.dump(self.dict(), fh, indent=4, sort_keys=True)
                    fh.close()
                    created = True
                else:
                    raise e
            except Exception:
                log.error("Error opening settings from path: %s" % path)
                return None

        # This should work now
        return open(path, mode), created

    def load(self, path: str, create: bool = True):
        global config_path  # Don't want in model.
        fh, created = self.__get_file(path, "r", create)
        config_path = path
        if created:
            # A config written from the current defaults is already current;
            # stamp it so _migrate() never re-runs against a fresh install.
            self.config_version = CONFIG_VERSION
            self.save()
        if not created:
            try:
                data = json.load(fh)
                safe_data = self.parse_obj(data)

                # Copy and count items
                input_params = 0
                for key in safe_data.__fields_set__:
                    setattr(self, key, getattr(safe_data, key))
                    input_params += 1

                # Print warnings
                for key, value in data.items():
                    if key not in safe_data.__fields_set__:
                        log.warning("Config item {0} was ignored.".format(key))
                    elif value != getattr(safe_data, key):
                        log.warning(
                            "Config item {0} was was coerced from {1} to {2}.".format(
                                key, repr(value), repr(getattr(safe_data, key))
                            )
                        )

                log.info("Loaded settings from json: %s" % path)
                migrated = self._migrate()
                if migrated or input_params < len(self.__fields__):
                    log.info("Saving back due to schema change.")
                    self.save()
            except Exception as e:
                log.error("Error loading settings from json: %s" % e)
                fh.close()
                return False

        fh.close()
        return True

    def save(self):
        if config_path is None:
            raise FileNotFoundError("Config path not set.")

        # Write-temp-then-rename under a lock: concurrent savers can't
        # interleave, and a crash mid-write leaves the old file intact
        # instead of a truncated one.
        with _save_lock:
            tmp = config_path + ".tmp"
            try:
                with open(tmp, "w") as fh:
                    json.dump(self.dict(), fh, indent=4, sort_keys=True)
                    fh.flush()
                os.replace(tmp, config_path)
            except Exception as e:
                log.error("Error saving settings to json: %s" % e)
                return False

        return True


settings = Settings()
