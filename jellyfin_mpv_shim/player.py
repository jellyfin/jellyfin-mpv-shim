import logging
import os
import sys
import time
import platform

from threading import RLock, Lock, Event
from queue import Queue
from collections import OrderedDict
from typing import TYPE_CHECKING, Optional

from . import conffile
from .utils import synchronous, Timer, none_fallback, get_resource
from .conf import settings
from .menu import OSDMenu
from .constants import APP_NAME
from .syncplay import SyncPlayManager
from .update_check import UpdateChecker
from .i18n import _

if TYPE_CHECKING:
    from .media import Video as Video_type

log = logging.getLogger("player")
mpv_log = logging.getLogger("mpv")


discord_presence = False
if settings.discord_presence:
    try:
        from .rich_presence import register_join_event, send_presence, clear_presence

        discord_presence = True
    except Exception:
        log.error("Could not enable Discord Rich Presence.", exc_info=True)

python_mpv_available = True
is_using_ext_mpv = False
if not settings.mpv_ext:
    try:
        # noinspection PyPackageRequirements
        import mpv

        log.info("Using libmpv playback backend.")
    except OSError:
        log.warning("Could not find libmpv.")
        python_mpv_available = False

if settings.mpv_ext or not python_mpv_available:
    import python_mpv_jsonipc as mpv

    log.info("Using external mpv playback backend.")
    is_using_ext_mpv = True

SUBTITLE_POS = {
    "top": 0,
    "bottom": 100,
    "middle": 80,
}

mpv_log_levels = {
    "fatal": mpv_log.error,
    "error": mpv_log.error,
    "warn": mpv_log.warning,
    "info": mpv_log.info,
}


def mpv_log_handler(level: str, prefix: str, text: str):
    if level in mpv_log_levels:
        mpv_log_levels[level]("{0}: {1}".format(prefix, text))
    else:
        mpv_log.debug("{0}: {1}".format(prefix, text))


def wait_property(
    instance, name: str, cond=lambda x: True, timeout: Optional[int] = None
):
    success = True
    event = Event()

    def handler(_name, value):
        if cond(value):
            event.set()

    if is_using_ext_mpv:
        observer_id = instance.bind_property_observer(name, handler)
        if timeout:
            success = event.wait(timeout=timeout)
        else:
            event.wait()
        instance.unbind_property_observer(observer_id)
    else:
        instance.observe_property(name, handler)
        if timeout:
            success = event.wait(timeout=timeout)
        else:
            event.wait()
        instance.unobserve_property(name, handler)
    return success


win_utils = None
if sys.platform.startswith("win32") or sys.platform.startswith("cygwin"):
    try:
        from . import win_utils
    except ModuleNotFoundError:
        log.warning("win_utils is not available.")

# Q: What is with the put_task call?
# A: Some calls to python-mpv require event processing.
#    put_task is used to deal with the events originating from
#    the event thread, which would cause deadlock if they run there.


# noinspection PyUnresolvedReferences
class PlayerManager(object):
    """
    The underlying player is thread safe, however, locks are used in this
    class to prevent concurrent control events hitting the player, which
    violates assumptions.
    """

    def __init__(self):
        self._video = None
        mpv_options = OrderedDict()
        mpv_location = settings.mpv_ext_path
        # Use bundled path for MPV if not specified by user, on Mac OS, and frozen
        if (
            mpv_location is None
            and platform.system() == "Darwin"
            and getattr(sys, "frozen", False)
        ):
            mpv_location = get_resource("mpv")
        self.timeline_trigger = None
        self.action_trigger = None
        self.external_subtitles = {}
        self.external_subtitles_rev = {}
        self.should_send_timeline = False
        self.start_time = None
        self.url = None
        self.evt_queue = Queue()
        self._lock = RLock()
        self._tl_lock = RLock()
        self._finished_lock = Lock()
        self.last_update = Timer()
        self._jf_settings = None
        self.get_webview = lambda: None
        self.pause_ignore = None  # Used to ignore pause events that come from us.
        self.do_not_handle_pause = False
        self.last_seek = None
        self.warned_about_transcode = False
        self.fullscreen_disable = False
        self.update_check = UpdateChecker(self)
        self.is_in_intro = False
        self.trickplay = None

        if is_using_ext_mpv:
            mpv_options.update(
                {
                    "start_mpv": settings.mpv_ext_start,
                    "ipc_socket": settings.mpv_ext_ipc,
                    "mpv_location": mpv_location,
                    "player-operation-mode": "cplayer",
                }
            )

        scripts = []
        if settings.menu_mouse:
            scripts.append(get_resource("mouse.lua"))
        if settings.thumbnail_enable:
            try:
                from .trickplay import TrickPlay

                self.trickplay = TrickPlay(self)
                self.trickplay.start()

                scripts.append(get_resource("thumbfast.lua"))
                if settings.thumbnail_osc_builtin:
                    scripts.append(get_resource("trickplay-osc.lua"))

            except Exception:
                log.error("Could not enable trickplay.", exc_info=True)

            mpv_options["osc"] = False

        # ensure standard mpv configuration directories and files exist
        conffile.get_dir(APP_NAME, "scripts")
        conffile.get_dir(APP_NAME, "fonts")
        conffile.get(APP_NAME, "input.conf", True)
        conffile.get(APP_NAME, "mpv.conf", True)

        if scripts:
            if settings.mpv_ext:
                mpv_options["script"] = scripts
            else:
                mpv_options["scripts"] = (
                    ";" if sys.platform.startswith("win32") else ":"
                ).join(scripts)

        if not (settings.mpv_ext and settings.mpv_ext_no_ovr):
            mpv_options["config"] = True
            mpv_options["config_dir"] = conffile.confdir(APP_NAME)

        if settings.tls_client_cert and settings.tls_client_key:
            mpv_options['tls_cert_file'] = settings.tls_client_cert
            mpv_options['tls_key_file'] = settings.tls_client_key

            if settings.tls_server_ca:
                mpv_options['tls_ca_file'] = settings.tls_server_ca

        self._player = mpv.MPV(
            input_default_bindings=True,
            input_vo_keyboard=True,
            input_media_keys=settings.media_keys,
            log_handler=mpv_log_handler,
            loglevel=settings.mpv_log_level,
            **mpv_options,
        )

        self.menu = OSDMenu(self, self._player)
        self.syncplay = SyncPlayManager(self)

        if discord_presence:
            try:
                register_join_event(self.syncplay.discord_join_group)
            except Exception:
                log.error("Could not register Discord join callback.", exc_info=True)

        if hasattr(self._player, "osc"):
            self.enable_osc(settings.enable_osc)
        else:
            log.warning("This mpv version doesn't support on-screen controller.")

        if settings.screenshot_dir is not None:
            if hasattr(self._player, "screenshot_directory"):
                self._player.screenshot_directory = settings.screenshot_dir
            else:
                log.warning(
                    "This mpv version doesn't support setting the screenshot directory."
                )

        if hasattr(self._player, "resume_playback"):
            # This can lead to unwanted skipping of videos
            self._player.resume_playback = False

        # Wrapper for on_key_press that ignores None.
        def keypress(key):
            def wrapper(func):
                if key is not None:
                    self._player.on_key_press(key)(func)
                return func

            return wrapper

        @self._player.on_key_press("CLOSE_WIN")
        @self._player.on_key_press("STOP")
        @keypress(settings.kb_stop)
        def handle_stop():
            self.stop()

        @keypress(settings.kb_prev)
        def handle_prev():
            self.put_task(self.play_prev)

        @keypress(settings.kb_next)
        def handle_next():
            self.put_task(self.play_next)

        @self._player.on_key_press("PREV")
        @self._player.on_key_press("XF86_PREV")
        def handle_media_prev():
            if settings.media_key_seek:
                seektime, _x = self.get_seek_times()
                self.seek(seektime)
            else:
                self.put_task(self.play_prev)

        @self._player.on_key_press("NEXT")
        @self._player.on_key_press("XF86_NEXT")
        def handle_media_next():
            if settings.media_key_seek:
                if self.is_in_intro:
                    self.skip_intro()
                else:
                    _x, seektime = self.get_seek_times()
                    self.seek(seektime)
            else:
                self.put_task(self.play_next)

        @keypress(settings.kb_watched)
        def handle_watched():
            self.put_task(self.watched_skip)

        @keypress(settings.kb_unwatched)
        def handle_unwatched():
            self.put_task(self.unwatched_quit)

        @keypress(settings.kb_menu)
        def menu_open():
            if not self.menu.is_menu_shown:
                self.menu.show_menu()
            else:
                self.menu.hide_menu()

        @keypress(settings.kb_menu_esc)
        def menu_back():
            if self.menu.is_menu_shown:
                self.menu.menu_action("back")
            else:
                self._player.command("set", "fullscreen", "no")
                self.fullscreen_disable = True

        @keypress(settings.kb_menu_ok)
        def menu_ok():
            self.menu.menu_action("ok")

        @keypress(settings.kb_menu_left)
        def menu_left():
            if self.menu.is_menu_shown:
                self.menu.menu_action("left")
            else:
                self.kb_seek("left")

        @keypress(settings.kb_menu_right)
        def menu_right():
            if self.menu.is_menu_shown:
                self.menu.menu_action("right")
            else:
                if self.is_in_intro:
                    self.skip_intro()
                else:
                    self.kb_seek("right")

        @keypress(settings.kb_menu_up)
        def menu_up():
            if self.menu.is_menu_shown:
                self.menu.menu_action("up")
            else:
                if self.is_in_intro:
                    self.skip_intro()
                else:
                    self.kb_seek("up")

        @keypress(settings.kb_menu_down)
        def menu_down():
            if self.menu.is_menu_shown:
                self.menu.menu_action("down")
            else:
                self.kb_seek("down")

        @keypress(settings.kb_pause)
        def handle_pause():
            if self.menu.is_menu_shown:
                self.menu.menu_action("ok")
            else:
                self.toggle_pause()

        @keypress(settings.kb_fullscreen)
        def handle_fullscreen():
            self.toggle_fullscreen()

        # This gives you an interactive python debugger prompt.
        @keypress(settings.kb_debug)
        def handle_debug():
            import pdb

            pdb.set_trace()

        # Kill shader packs (useful for breakage)
        @keypress(settings.kb_kill_shader)
        def kill_shaders():
            if settings.shader_pack_remember:
                settings.shader_pack_profile = None
                settings.save()
            if self.menu.profile_manager is not None:
                self.menu.profile_manager.unload_profile()

        # Fires between episodes.
        @self._player.property_observer("eof-reached")
        def handle_end(_name, reached_end: bool):
            self.pause_ignore = True
            if self._video and reached_end:
                has_lock = self._finished_lock.acquire(False)
                self.put_task(self.finished_callback, has_lock)

        # Fires at the end.
        @self._player.property_observer("playback-abort")
        def handle_end_idle(_name, value: bool):
            self.pause_ignore = True
            if self._video and value and not self._video.parent.has_next:
                has_lock = self._finished_lock.acquire(False)
                self.put_task(self.finished_callback, has_lock)

        @self._player.property_observer("seeking")
        def handle_seeking(_name, value: bool):
            if self.do_not_handle_pause:
                return

            if self.syncplay.is_enabled():
                play_time = self._player.playback_time
                if (
                    play_time is not None
                    and self.last_seek is not None
                    and abs(self.last_seek - play_time) > 10
                ):
                    self.syncplay.seek_request(play_time)
                else:
                    log.info("SyncPlay Buffering: {0}".format(value))
                    if value:
                        self.syncplay.on_buffer()
                    else:
                        self.syncplay.on_buffer_done()

        @self._player.property_observer("pause")
        def pause_handler(_name, value: bool):
            if self.do_not_handle_pause:
                return

            if not self._player.playback_abort:
                self.timeline_handle()

            if value != self.pause_ignore:
                if self.syncplay.is_enabled():
                    if value:
                        self.syncplay.pause_request()
                    else:
                        # Don't allow unpausing locally through MPV.
                        self.syncplay.play_request()
                        self.set_paused(True, True)

        @self._player.event_callback("client-message")
        def handle_client_message(event):
            try:
                # Python-MPV 1.0 uses a class/struct combination now
                if hasattr(event, "as_dict"):
                    event = event.as_dict()
                    if "event" in event:
                        event["event"] = event["event"].decode("utf-8")
                    if "args" in event:
                        event["args"] = [d.decode("utf-8") for d in event["args"]]

                if "event_id" in event:
                    args = event["event"]["args"]
                else:
                    args = event["args"]
                if len(args) == 0:
                    return
                if args[0] == "shim-menu-select":
                    # Apparently this can happen...
                    if args[1] == "inf":
                        return
                    self.menu.mouse_select(int(args[1]))
                elif args[0] == "shim-menu-click":
                    self.menu.menu_action("ok")
            except Exception:
                log.warning("Error when processing client-message.", exc_info=True)

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

    def skip_intro(self):
        _, intro = self._video.get_current_intro(self._player.playback_time)

        self._player.playback_time = intro.end
        intro.has_triggered = True
        self.timeline_handle()
        self.is_in_intro = False

    @synchronous("_lock")
    def update(self):
        if (
            (
                settings.skip_intro_always
                or settings.skip_intro_enable
                or settings.skip_credits_always
                or settings.skip_credits_enable
            )
            and not self.syncplay.is_enabled()
            and self._video is not None
            and self._player.playback_time is not None
        ):
            ready_to_skip, intro = self._video.get_current_intro(
                self._player.playback_time
            )

            if intro is not None:
                should_prompt = (
                    intro.type != "Outro" and settings.skip_intro_enable
                ) or (intro.type == "Outro" and settings.skip_credits_enable)
                should_skip = (not intro.has_triggered) and (
                    (intro.type != "Outro" and settings.skip_intro_always)
                    or (intro.type == "Outro" and settings.skip_credits_always)
                )

                if should_skip and ready_to_skip:
                    intro.has_triggered = True
                    self.skip_intro()
                    self._player.show_text(
                        _("Skipped Credits")
                        if intro.type == "Outro"
                        else _("Skipped Intro"),
                        3000,
                        1,
                    )

                if not self.is_in_intro and should_prompt:
                    self._player.show_text(
                        _("Seek to Skip Credits")
                        if intro.type == "Outro"
                        else _("Seek to Skip Intro"),
                        3000,
                        1,
                    )
                self.is_in_intro = True
            else:
                self.is_in_intro = False

        while not self.evt_queue.empty():
            func, args = self.evt_queue.get()
            func(*args)
        if self._video and not self._player.playback_abort:
            if not self.is_paused():
                self.last_update.restart()

    def play(
        self,
        video: "Video_type",
        offset: int = 0,
        no_initial_timeline: bool = False,
        is_initial_play: bool = False,
    ):
        self.should_send_timeline = False
        self.start_time = time.time()
        url = video.get_playback_url()
        if not url:
            log.error("PlayerManager::play no URL found")
            return

        self._play_media(video, url, offset, no_initial_timeline, is_initial_play)

    @synchronous("_lock")
    def _play_media(
        self,
        video: "Video_type",
        url: str,
        offset: int = 0,
        no_initial_timeline: bool = False,
        is_initial_play: bool = False,
    ):
        self.pause_ignore = True
        self.do_not_handle_pause = True
        self.url = url
        self.menu.hide_menu()

        if self.trickplay:
            self.trickplay.clear()

        if settings.log_decisions:
            log.info("Playing: {0}".format(url))
        if self.get_webview() is not None and settings.display_mirroring:
            # noinspection PyUnresolvedReferences
            self.get_webview().hide()

        self._player.play(self.url)
        if not wait_property(
            self._player, "duration", lambda x: x is not None, settings.playback_timeout
        ):
            # Timeout playback attempt after 10 seconds
            log.error("Timeout when waiting for media duration. Stopping playback!")
            self.stop()
            return
        log.info("Finished waiting for media duration.")
        if settings.fullscreen and not self.fullscreen_disable:
            self._player.fs = True
        self._player.force_media_title = video.get_proper_title()
        self._video = video
        self.is_in_intro = False
        self.external_subtitles = {}
        self.external_subtitles_rev = {}

        self.upd_player_hide()
        self.configure_streams()
        self.update_subtitle_visuals()

        if win_utils and settings.raise_mpv and is_initial_play:
            win_utils.raise_mpv()

        if offset is not None and offset > 0:
            self.last_seek = offset
            self._player.playback_time = offset

        if not no_initial_timeline:
            self.send_timeline_initial()
        else:
            self.send_timeline()

        if self.syncplay.is_enabled():
            self.set_speed(1)
            self.syncplay.play_done()
        else:
            self.set_paused(False, False)

        if self.trickplay:
            self.trickplay.fetch_thumbnails()

        self.should_send_timeline = True
        self.do_not_handle_pause = False
        if self._finished_lock.locked():
            self._finished_lock.release()

        self.update_check.check()

        if (
            not self._video.parent.is_local
            and self._video.is_transcode
            and not self.warned_about_transcode
            and settings.transcode_warning
        ):
            self.warned_about_transcode = True
            self._player.show_text(
                _(
                    "Your remote video is transcoding!\nPress c to adjust bandwidth settings if this is not needed."
                ),
                5000,
                1,
            )

    @staticmethod
    def exec_stop_cmd():
        if settings.stop_cmd:
            os.system(settings.stop_cmd)

    @synchronous("_lock")
    def stop(self):
        if self.syncplay.is_enabled():
            self.syncplay.disable_sync_play(False)

        if not self._video or self._player.playback_abort:
            self.exec_stop_cmd()
            return

        log.info("PlayerManager::stop stopping playback of %s" % self._video)

        self.should_send_timeline = False
        options = self.get_timeline_options()
        self.set_paused(False)
        local_video = self._video
        self._video = None
        self._player.command("stop")
        local_video.terminate_transcode()
        self.send_timeline_stopped(options=options, client=local_video.client)
        self.exec_stop_cmd()

        if self.trickplay:
            self.trickplay.clear()

    def get_volume(self, percent: bool = False):
        if self._player:
            if not percent:
                return self._player.volume / 100
            return self._player.volume

    @synchronous("_lock")
    def toggle_pause(self):
        if not self._player.playback_abort:
            self.set_paused(not self._player.pause)

    @synchronous("_lock")
    def pause_if_playing(self):
        if not self._player.playback_abort:
            if not self._player.pause:
                self.set_paused(True)
        self.timeline_handle()

    @synchronous("_lock")
    def play_if_paused(self):
        if not self._player.playback_abort:
            if self._player.pause:
                self.set_paused(False)
        self.timeline_handle()

    @synchronous("_lock")
    def seek(
        self,
        offset: float,
        absolute: bool = False,
        force: bool = False,
        exact: Optional[bool] = None,
    ):
        """
        Seek to ``offset`` seconds
        """
        if exact is None:
            exact = absolute
        if self.syncplay.is_enabled() and not force:
            if not absolute:
                offset += self._player.playback_time
            self.syncplay.seek_request(offset)
        else:
            if not self._player.playback_abort:
                if absolute:
                    if self.syncplay.is_enabled():
                        self.last_seek = offset
                    if self.is_in_intro and offset > self._player.playback_time:
                        self.skip_intro()
                    p2 = "absolute"
                    if exact:
                        p2 += "+exact"
                    self._player.command("seek", offset, p2)
                else:
                    if self.syncplay.is_enabled():
                        self.last_seek = self._player.playback_time + offset
                    if (
                        self.is_in_intro
                        and self._player.playback_time + offset
                        > self._player.playback_time
                    ):
                        self.skip_intro()
                    if exact:
                        self._player.command("seek", offset, "exact")
                    else:
                        self._player.command("seek", offset)
        self.timeline_handle()

    @synchronous("_lock")
    def set_volume(self, pct: float):
        if not self._player.playback_abort:
            self._player.volume = pct
        self.timeline_handle()

    @synchronous("_lock")
    def get_state(self):
        if self._player.playback_abort:
            return "stopped"

        if self._player.pause:
            return "paused"

        return "playing"

    @synchronous("_lock")
    def is_paused(self):
        if not self._player.playback_abort:
            return self._player.pause
        return False

    @synchronous("_lock")
    def finished_callback(self, has_lock: bool):
        if not self._video:
            self.pause_ignore = False
            return

        if settings.force_set_played:
            self._video.set_played()
        if self._video.parent.has_next and settings.auto_play:
            if has_lock:
                log.info("PlayerManager::finished_callback starting next episode")
                new_video = self._video.parent.get_next().video
                self.send_timeline_stopped(True)
                if self.syncplay.is_enabled():
                    self.syncplay.request_next(self._video.get_playlist_id())
                else:
                    self.play(new_video)
            else:
                log.info("PlayerManager::finished_callback No lock, skipping...")
        else:
            if settings.media_ended_cmd:
                os.system(settings.media_ended_cmd)

            if self.syncplay.is_enabled():
                self.syncplay.disable_sync_play(False)

            log.info("PlayerManager::finished_callback reached end")
            self.send_timeline_stopped(True)
        self.pause_ignore = False

    @synchronous("_lock")
    def watched_skip(self):
        if not self._video:
            return

        self._video.set_played()
        self.play_next()

    @synchronous("_lock")
    def unwatched_quit(self):
        if not self._video:
            return

        video = self._video
        self.stop()
        video.set_played(False)

    @synchronous("_lock")
    def play_next(self):
        if self._video.parent.has_next:
            new_video = self._video.parent.get_next().video
            self.send_timeline_stopped(True)
            if self.syncplay.is_enabled():
                self.syncplay.request_next(self._video.get_playlist_id())
            else:
                self.play(new_video)
            return True
        return False

    @synchronous("_lock")
    def skip_to(self, key: str):
        media = self._video.parent.get_from_key(key)
        if media:
            self.send_timeline_stopped(True)
            if self.syncplay.is_enabled():
                self.syncplay.request_skip(media.video.get_playlist_id())
            else:
                self.play(media.get_video(0))
            return True
        return False

    @synchronous("_lock")
    def play_prev(self):
        if self._video.parent.has_prev:
            new_video = self._video.parent.get_prev().video
            self.send_timeline_stopped(True)
            if self.syncplay.is_enabled():
                self.syncplay.request_prev(self._video.get_playlist_id())
            else:
                self.play(new_video)
            return True
        return False

    @synchronous("_lock")
    def restart_playback(self):
        current_time = self._player.playback_time
        self.play(self._video, current_time)
        return True

    @synchronous("_lock")
    def get_video_attr(self, attr: str, default=None):
        if self._video:
            return self._video.get_video_attr(attr, default)
        return default

    @synchronous("_lock")
    def configure_streams(self):
        audio_uid = self._video.aid
        sub_uid = self._video.sid

        if audio_uid is not None and not self._video.is_transcode:
            log.info("PlayerManager::play selecting audio stream index=%s" % audio_uid)
            self._player.audio = self._video.audio_seq[audio_uid]

        if sub_uid is None or sub_uid == -1:
            log.info("PlayerManager::play selecting subtitle stream (none)")
            self._player.sub = "no"
        else:
            log.info(
                "PlayerManager::play selecting subtitle stream index=%s" % sub_uid
            )
            if sub_uid in self._video.subtitle_seq:
                self._player.sub = self._video.subtitle_seq[sub_uid]
            elif sub_uid in self._video.subtitle_url:
                log.info(
                    "PlayerManager::play selecting external subtitle id=%s" % sub_uid
                )
                self.load_external_sub(sub_uid)

    @synchronous("_lock")
    def set_streams(self, audio_uid: int, sub_uid: int):
        need_restart = self._video.set_streams(audio_uid, sub_uid)

        if need_restart:
            self.restart_playback()
        else:
            self.configure_streams()
        self.timeline_handle()

    @synchronous("_lock")
    def load_external_sub(self, sub_id: int):
        if sub_id in self.external_subtitles:
            self._player.sub = self.external_subtitles[sub_id]
        else:
            try:
                sub_url = self._video.subtitle_url[sub_id]
                if settings.log_decisions:
                    log.info("Load External Subtitle: {0}".format(sub_url))
                self._player.sub_add(sub_url)
                self.external_subtitles[sub_id] = self._player.sub
                self.external_subtitles_rev[self._player.sub] = sub_id
            except SystemError:
                log.info("PlayerManager::could not load external subtitle")

    @synchronous("_lock")
    def toggle_fullscreen(self):
        self._player.fs = not self._player.fs
        self.fullscreen_disable = not self._player.fs

    @synchronous("_lock")
    def set_fullscreen(self, enabled: bool):
        self._player.fs = enabled
        self.fullscreen_disable = not enabled

    @synchronous("_lock")
    def set_mute(self, mute):
        self._player.mute = mute

    @synchronous("_lock")
    def screenshot(self):
        self._player.screenshot()

    @synchronous("_lock")
    def set_paused(self, value: bool, force: bool = False):
        if self.syncplay.is_enabled() and not force:
            if value:
                self.syncplay.pause_request()
            else:
                self.syncplay.play_request()
        else:
            self.pause_ignore = value
            self._player.pause = value

    @synchronous("_lock")
    def script_message(self, command, *args):
        self._player.command("script-message", command, *args)

    def get_track_ids(self):
        return self._video.aid, self._video.sid

    def update_subtitle_visuals(self):
        self._player.sub_pos = SUBTITLE_POS[settings.subtitle_position]
        self._player.sub_scale = settings.subtitle_size / 100
        self._player.sub_color = settings.subtitle_color
        self.timeline_handle()

    def get_timeline_options(self, finished=False):
        # PlaylistItemId is dynamically generated. A more stable Id will be used
        # if queue manipulation is added as a feature.
        player = self._player
        if finished:
            safe_pos = self._video.get_duration() or 0
        else:
            safe_pos = player.playback_time or 0
        self.last_seek = safe_pos
        self.pause_ignore = player.pause
        options = {
            "VolumeLevel": int(player.volume or 100),
            "IsMuted": player.mute,
            "IsPaused": player.pause,
            "RepeatMode": "RepeatNone",
            # "MaxStreamingBitrate": 140000000,
            "PositionTicks": int(safe_pos * 10000000),
            "PlaybackStartTimeTicks": int(self.start_time * 10000000),
            "SubtitleStreamIndex": none_fallback(self._video.sid, -1),
            "AudioStreamIndex": none_fallback(self._video.aid, -1),
            "BufferedRanges": [],
            "PlayMethod": "Transcode" if self._video.is_transcode else "DirectPlay",
            "PlaySessionId": self._video.playback_info["PlaySessionId"],
            "PlaylistItemId": self._video.get_playlist_id(),
            "MediaSourceId": self._video.media_source["Id"],
            "CanSeek": True,
            "ItemId": self._video.item_id,
            "NowPlayingQueue": self._video.parent.queue,
        }
        if player.duration is not None:
            options["BufferedRanges"] = [
                {
                    "start": int(safe_pos * 10000000),
                    "end": int(
                        (
                            (
                                player.duration
                                - safe_pos
                                * none_fallback(player.cache_buffering_state, 0)
                                / 100
                            )
                            + safe_pos
                        )
                        * 10000000
                    ),
                }
            ]
        if discord_presence:
            try:
                if (
                    self._video.is_tv
                    and self._video.item.get("IndexNumber") is not None
                    and self._video.item.get("ParentIndexNumber") is not None
                ):
                    title = self._video.item.get("SeriesName")
                    subtitle = _("Season {0} - Episode {1}").format(
                        self._video.item.get("ParentIndexNumber"),
                        self._video.item.get("IndexNumber"),
                    )
                else:
                    title = self._video.item.get("Name")
                    subtitle = str(self._video.item.get("ProductionYear", ""))
                send_presence(
                    title,
                    subtitle,
                    player.playback_time,
                    player.duration,
                    not player.pause,
                    self.syncplay.current_group,
                )
            except Exception:
                log.error("Could not send Discord Rich Presence.", exc_info=True)
        return options

    @synchronous("_tl_lock")
    def send_timeline(self):
        if (
            self.should_send_timeline
            and self._video
            and not self._player.playback_abort
        ):
            self._video.client.jellyfin.session_progress(self.get_timeline_options())
            try:
                if self.syncplay.is_enabled():
                    self.syncplay.sync_playback_time()
            except:
                log.error("Error syncing playback time.", exc_info=True)

    @synchronous("_tl_lock")
    def send_timeline_initial(self):
        self._video.client.jellyfin.session_playing(self.get_timeline_options())

    @synchronous("_tl_lock")
    def send_timeline_stopped(self, finished=False, options=None, client=None):
        self.should_send_timeline = False

        if options is None:
            options = self.get_timeline_options(finished)

        if client is None:
            client = self._video.client

        client.jellyfin.session_stop(options)

        if self.get_webview() is not None and settings.display_mirroring:
            self.get_webview().show()

        if discord_presence:
            try:
                clear_presence()
            except Exception:
                log.error("Could not clear Discord Rich Presence.", exc_info=True)

    def upd_player_hide(self):
        if self._video:
            self._player.keep_open = self._video.parent.has_next

    def terminate(self):
        self.stop()
        if is_using_ext_mpv:
            self._player.terminate()

        if self.trickplay:
            self.trickplay.stop()

    def get_seek_times(self):
        if self._jf_settings is None:
            self._jf_settings = self._video.client.jellyfin.get_user_settings()
        custom_prefs = self._jf_settings.get("CustomPrefs") or {}
        seek_left = custom_prefs.get("skipBackLength") or 15000
        seek_right = custom_prefs.get("skipForwardLength") or 30000
        return -int(seek_left) / 1000, int(seek_right) / 1000

    # Wrappers to avoid private access
    def is_active(self):
        return bool(self._player and self._video)

    def is_playing(self):
        return bool(self._video and not self._player.playback_abort)

    def is_not_paused(self):
        return bool(
            self._video and not self._player.playback_abort and not self._player.pause
        )

    def has_video(self):
        return self._video is not None

    def get_video(self):
        return self._video

    def show_text(self, text: str, duration: int, level: int = 1):
        self._player.show_text(text, duration, level)

    def get_osd_settings(self):
        return self._player.osd_back_color, self._player.osd_font_size

    def set_osd_settings(self, back_color: str, font_size: int):
        self._player.osd_back_color = back_color
        self._player.osd_font_size = font_size

    def enable_osc(self, enabled: bool):
        if settings.thumbnail_enable and self.trickplay:
            self.script_message(
                "osc-visibility", "auto" if enabled else "never", "False"
            )
        else:
            if hasattr(self._player, "osc"):
                self._player.osc = enabled

    def triggered_menu(self, enabled: bool):
        self.script_message("shim-menu-enable", "True" if enabled else "False")

    def playback_is_aborted(self):
        return self._player.playback_abort

    def force_window(self, enabled: bool):
        if enabled:
            self._player.force_window = True
            self._player.keep_open = True
            self._player.play("")
            if settings.fullscreen:
                self._player.fs = True
        else:
            self._player.keep_open = False
            if self._player.playback_abort:
                self._player.force_window = False
                self._player.play("")
            else:
                self.upd_player_hide()

    def add_ipc(self, ipc_name: str):
        self._player.input_ipc_server = ipc_name

    def get_current_client(self):
        return self._video.client

    def get_time(self):
        return self._player.playback_time

    def get_speed(self):
        return self._player.speed

    def set_speed(self, speed: float):
        self._player.speed = speed

    def kb_seek(self, action):
        if action == "up":
            self.seek(settings.seek_up, exact=settings.seek_v_exact)
        elif action == "down":
            self.seek(settings.seek_down, exact=settings.seek_v_exact)
        elif action == "left":
            seektime = settings.seek_left
            if settings.use_web_seek:
                seektime, _x = self.get_seek_times()
            self.seek(seektime, exact=settings.seek_h_exact)
        elif action == "right":
            seektime = settings.seek_right
            if settings.use_web_seek:
                _x, seektime = self.get_seek_times()
            self.seek(seektime, exact=settings.seek_h_exact)
        else:
            self.menu.menu_action(action)

    def menu_action(self, action):
        if self.menu.is_menu_shown:
            self.menu.menu_action(action)
        else:
            self.kb_seek(action)


playerManager = PlayerManager()
