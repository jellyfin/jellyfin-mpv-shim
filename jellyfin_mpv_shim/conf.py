import logging
import os
import uuid
import socket
import json
import os.path
import sys
import getpass
from typing import Optional
from .settings_base import SettingsBase

log = logging.getLogger("conf")
config_path = None


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
    player_name: str = socket.gethostname()
    audio_output: str = "hdmi"
    client_uuid: str = str(uuid.uuid4())
    media_ended_cmd: Optional[str] = None
    pre_media_cmd: Optional[str] = None
    stop_cmd: Optional[str] = None
    auto_play: bool = True
    idle_cmd: Optional[str] = None
    idle_ended_cmd: Optional[str] = None
    play_cmd: Optional[str] = None
    idle_cmd_delay: int = 60
    direct_paths: bool = False
    remote_direct_paths: bool = False
    always_transcode: bool = False
    transcode_hi10p: bool = False
    transcode_hdr: bool = False
    transcode_hevc: bool = False
    transcode_av1: bool = False
    transcode_4k: bool = False
    transcode_dolby_vision: bool = True
    allow_transcode_to_h265: bool = False
    prefer_transcode_to_h265: bool = False
    remote_kbps: int = 10000
    local_kbps: int = 2147483
    subtitle_size: int = 100
    subtitle_color: str = "#FFFFFFFF"
    subtitle_position: str = "bottom"
    fullscreen: bool = True
    enable_gui: bool = True
    media_key_seek: bool = False
    mpv_ext: bool = sys.platform.startswith("darwin")
    mpv_ext_path: Optional[str] = None
    mpv_ext_ipc: Optional[str] = None
    mpv_ext_start: bool = True
    mpv_ext_no_ovr: bool = False
    enable_osc: bool = True
    use_web_seek: bool = False
    display_mirroring: bool = False
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
    thumbnail_enable: bool = True
    thumbnail_osc_builtin: bool = True
    thumbnail_preferred_size: int = 320
    tls_client_cert: Optional[str] = None
    tls_client_key: Optional[str] = None
    tls_server_ca: Optional[str] = None

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
                if input_params < len(self.__fields__):
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

        # noinspection PyTypeChecker
        fh, created = self.__get_file(config_path, "w", True)

        try:
            json.dump(self.dict(), fh, indent=4, sort_keys=True)
            fh.flush()
            fh.close()
        except Exception as e:
            log.error("Error saving settings to json: %s" % e)
            return False

        return True


settings = Settings()
