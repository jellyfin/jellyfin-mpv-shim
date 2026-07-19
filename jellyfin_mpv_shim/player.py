import logging
import os
import sys
import time
import json

import platform

from threading import RLock, Lock, Thread, Event
from queue import Queue, Empty as queue_empty
from collections import OrderedDict
from typing import TYPE_CHECKING, Optional

from . import conffile
from .utils import synchronous, Timer, none_fallback, get_resource
from .mpv_events import wait_property
from .conf import settings
from .menu import OSDMenu
from .osc_bridge import OscBridge
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

# Collect backend-specific exceptions for MPV disconnection/shutdown.
# libmpv raises ShutdownError; external mpv (jsonipc) raises BrokenPipeError
# for a dead socket and TimeoutError for a wedged-but-alive mpv, which is
# just as unusable — treat both as a disconnect.
_mpv_errors = (BrokenPipeError,)
if hasattr(mpv, "ShutdownError"):
    _mpv_errors = (BrokenPipeError, mpv.ShutdownError)
else:
    _mpv_errors = (BrokenPipeError, TimeoutError)

_browse_bg_path = None


def _browse_background():
    """Path to a solid dark image used as the mpvtk browser's window
    background, replacing the Jellyfin logo splash that force_window()
    shows for the menu. Generated once and cached (the browser scales it to
    fill with keepaspect-window=no)."""
    global _browse_bg_path
    if _browse_bg_path and os.path.exists(_browse_bg_path):
        return _browse_bg_path
    try:
        import tempfile
        from PIL import Image

        path = os.path.join(tempfile.gettempdir(), "mpvtk-browse-bg.png")
        if not os.path.exists(path):
            Image.new("RGB", (16, 16), (20, 20, 20)).save(path)
        _browse_bg_path = path
        return path
    except Exception:
        # PIL missing (the browser needs it anyway) -> fall back to the logo:
        # the window still opens, just with branding.
        return get_resource("logo.png")

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


def _rank_stream(prev_source, prev_index, streams, stream_type):
    """Find the stream in `streams` best matching the previously-selected one
    (jellyfin-web heuristic): +2 language, +2 display title, +1 relative index,
    +1 codec; a match needs >= 3. Returns the matching stream Index or None."""
    prev_streams = [s for s in (prev_source.get("MediaStreams") or [])
                    if s.get("Type") == stream_type]
    prev_stream = next((s for s in prev_streams if s.get("Index") == prev_index),
                       None)
    if prev_stream is None:
        return None
    prev_rel = prev_streams.index(prev_stream)

    best_score, best_index = 0, None
    for rel, stream in enumerate(s for s in streams if s.get("Type") == stream_type):
        score = 0
        if prev_stream.get("Codec") and prev_stream.get("Codec") == stream.get("Codec"):
            score += 1
        if prev_rel == rel:
            score += 1
        title = prev_stream.get("DisplayTitle")
        if title and title == stream.get("DisplayTitle"):
            score += 2
        lang = prev_stream.get("Language")
        if lang and lang != "und" and lang == stream.get("Language"):
            score += 2
        if score > best_score and score >= 3:
            best_score, best_index = score, stream.get("Index")
    return best_index


# noinspection PyUnresolvedReferences
class PlayerManager(object):
    """
    The underlying player is thread safe, however, locks are used in this
    class to prevent concurrent control events hitting the player, which
    violates assumptions.
    """

    def __init__(self):
        self._video = None
        self.timeline_trigger = None
        self.action_trigger = None
        # (media_source, aid, sid) of the playing item, carried to the next
        # episode in the queue (jellyfin-web-style track matching).
        self._track_memory = None
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
        # Throttle for periodic offline resume-position persistence on the
        # timeline path (time.monotonic seconds); -inf so the first tick fires.
        self._last_offline_record = float("-inf")
        self.last_seek = None
        self.warned_about_transcode = False
        self.fullscreen_disable = False
        self.update_check = UpdateChecker(self)
        self.menu = None
        self.osc_bridge = OscBridge(self)
        self.is_in_intro = False
        self.playback_time_before_seek = None
        # time.time() of the last seek initiated from the jellyfin OSC's
        # own controls (seekbar/buttons); such seeks never intro-skip.
        self._last_ui_seek_time = 0.0
        self.trickplay = None
        # Decoded trickplay tile metadata for the CURRENT video, set by the
        # TrickPlay worker once tiles land ({count, multiplier, width,
        # height, file} — file is raw BGRA frames back to back). The mpvtk
        # playback HUD reads frames straight out of it for scrub previews;
        # the lua OSCs get the same data via shim-trickplay-bif instead.
        self.trickplay_meta = None
        # Skippable segment the mpvtk playback HUD should offer a button
        # for (an Intro object, or None). The lua OSC gets the same
        # prompt via osc_bridge.update_skip_button instead.
        self._hud_skip = None
        self._osc_script_loaded = False
        self._mpv_alive = False
        # True when mpv was terminated intentionally to save resources while
        # idle (mpv_idle_quit), as opposed to a crash / user-close. Lets the
        # shutdown path stay silent — there's no session to report. Cleared
        # when the process is re-created on the next play.
        self._idle_quit = False
        # The thread tearing down a previous mpv instance. A re-open joins it
        # before rebuilding: the old event thread must be dead before we drain
        # its leftover queued tasks (so none re-queue after the drain — see
        # _teardown_player), and for external mpv the new process must not grab
        # the ipc socket while the old one still holds it.
        self._terminate_thread = None
        # Playback generation, bumped each time a new file becomes current.
        # Queued finished-callbacks carry the epoch they were created under
        # and no-op if playback moved on before they ran.
        self._play_epoch = 0
        # True only when mpv reported a genuine end-of-file (eof-reached).
        # playback-abort fires on ANY abort — including decode/network failure —
        # so this flag is what distinguishes "watched to the end" from "the
        # stream died". Written by the mpv observer thread (handle_end), read by
        # the action/timeline threads; a plain bool is safe here (atomic
        # read/write in CPython, no compound state).
        self._reached_eof = False
        # Last known playback position; used when MPV exits (e.g. OSC 'x'
        # button) before we get to send the final timeline update.
        self._last_playback_position = 0
        # Timestamp of the most recent intro/credits prompt or skip toast.
        # Used to debounce the prompt loop so a skip event isn't immediately
        # overwritten by a "Seek to Skip Credits" prompt when the post-skip
        # position lands inside an outro segment (common on short videos).
        self._last_intro_msg_time = 0.0

        # Optional callback (set by gui_mgr) fed a compact now-playing dict on
        # every playback state change, for the browser's music bar. Kept as a
        # plain attribute so the player has no hard dependency on the GUI.
        self.on_playstate = None
        # Set True while the in-window mpvtk browser owns the window (browse
        # mode). Guards idle_quit so browsing never tears the window down, the
        # same way get_webview() guards it for the display mirror.
        self.mpvtk_active = False
        # True while the in-window browser's solid background image is the
        # loaded file. Guards against reloading it on top of itself, which
        # tears the video output down and back up (a visible window
        # close/reopen). Cleared whenever real media takes over.
        self._showing_browse_bg = False
        # Optional callback invoked when the user closes the mpv window while
        # the in-window UI owns it. Set by mpvtk_browser.ui, which decides
        # between minimizing to the tray and quitting. Unset -> stop_and_close.
        self.on_window_closed = None
        # mpv is torn down and re-created across idle-quit and crash recovery.
        # Anything holding the raw handle has to follow it — the OSD menu does
        # this via menu.update_player(); the in-window UI attaches a whole
        # renderer, so it gets explicit hooks. on_mpv_gone fires after the
        # handle is dead (free anything keyed to it — notably the in-process
        # BGRA tile buffers, which exist only to be read by that mpv);
        # on_mpv_recreated fires once a fresh handle is ready.
        self.on_mpv_gone = None
        self.on_mpv_recreated = None
        # BACK/ESC handler for the in-window UI. Returns True when it
        # consumed the press; at the root of its nav stack it declines and
        # ESC keeps its old meaning (leave fullscreen).
        self.on_nav_back = None
        # Remote menu commands the in-window UI answers itself ("home",
        # "settings"). Returns True when handled.
        self.on_nav_command = None
        # Optional callback (set by gui_mgr) invoked (version, url) when an
        # update is found, so the notice shows in the browser UI instead of on
        # the MPV OSD. Unset for CLI users -> update_check falls back to the OSD.
        self.notify_update = None
        # Repeat mode for the music bar: "none" | "all" | "one".
        self.repeat_mode = "none"
        # Set once the async session_playing has opened the server session;
        # send_timeline waits on it so progress can't precede the session open.
        self._session_ready = Event()
        self._session_ready.set()

        self._init_mpv()

    def _teardown_player(self):
        """Release process-scoped resources of the current mpv instance before
        a re-open (crash recovery / idle-quit). Safe to call before the first
        init — everything is None then.

        The trickplay worker is a *non-daemon* thread that would otherwise be
        leaked on every re-open (and block process exit); stopping it here is
        the fix. The mpv process itself is terminated by the disconnect path or
        the idle-quit path, not here."""
        # Wait for a previous instance's terminate to finish before a re-open
        # builds a new one — see _terminate_thread. Joining (not polling) also
        # avoids the libmpv segfault from touching a handle mid-teardown.
        term = self._terminate_thread
        if term is not None and term.is_alive():
            term.join(timeout=10)
            if term.is_alive():
                log.warning("Previous mpv terminate did not finish in time.")
        self._terminate_thread = None

        # Discard tasks the outgoing instance queued while tearing down. Its
        # shutdown/eof observers put_task _handle_mpv_shutdown and stray
        # finished_callbacks onto evt_queue; if they survive into the re-opened
        # session the pump runs them against the NEW video — _handle_mpv_shutdown
        # nulls self._video, after which the new player's eof is ignored and
        # auto-advance silently stops. The terminate join above guarantees the
        # old event thread is dead, so nothing re-queues after this drain.
        while True:
            try:
                self.evt_queue.get_nowait()
            except queue_empty:
                break

        if self.trickplay is not None:
            try:
                # join=False: _teardown_player runs under _lock and the
                # trickplay worker takes that lock in script_message, so
                # joining here would deadlock. It exits on its next loop turn.
                self.trickplay.stop(join=False)
            except Exception:
                log.debug("Stopping previous trickplay failed", exc_info=True)
            self.trickplay = None

    def _init_mpv(self):
        # Re-open reuses this method; drop the previous instance's trickplay
        # thread first so recovery/idle cycles don't leak it.
        # getattr: _player isn't bound until the first init finishes.
        reopen = getattr(self, "_player", None) is not None
        self._teardown_player()

        mpv_location = settings.mpv_ext_path
        if (
            mpv_location is None
            and platform.system() == "Darwin"
            and getattr(sys, "frozen", False)
        ):
            mpv_location = get_resource("mpv")

        mpv_options = OrderedDict()
        if is_using_ext_mpv:
            mpv_options.update(
                {
                    "start_mpv": settings.mpv_ext_start,
                    "ipc_socket": settings.mpv_ext_ipc,
                    "mpv_location": mpv_location,
                    "player-operation-mode": "cplayer",
                    "start_retries": settings.mpv_ext_start_retries,
                    "start_retry_delay_ms": settings.mpv_ext_start_retry_delay_ms,
                }
            )

        scripts = []
        if settings.menu_mouse:
            scripts.append(get_resource("mouse.lua"))

        # Which in-player UI to load: the jellyfin-styled OSC, the patched
        # stock OSC (trickplay previews), the in-window mpvtk playback HUD
        # (no lua script — the browser renders it; see mpvtk_browser/hud.py),
        # or none (whatever the mpv binary ships / the user's own scripts).
        osc_style = settings.osc_style
        if osc_style == "mpvtk" and settings.browser_ui != "mpvtk":
            # The playback HUD lives inside the mpvtk browser; without
            # that browser the jellyfin lua OSC is the closest equivalent.
            osc_style = "jellyfin"
        if osc_style == "jellyfin" and not settings.thumbnail_osc_builtin:
            # Legacy opt-out: thumbnail_osc_builtin=False used to mean
            # "don't replace my OSC" (e.g. users running uosc).
            osc_style = "default"

        if settings.thumbnail_enable:
            try:
                from .trickplay import TrickPlay

                self.trickplay = TrickPlay(self)
                self.trickplay.start()

                # Loaded regardless of OSC style: both shim OSCs consume
                # it, and thumbfast-aware user OSCs (e.g. uosc) benefit
                # under "default" too.
                scripts.append(get_resource("thumbfast.lua"))
            except Exception:
                log.error("Could not enable trickplay.", exc_info=True)

        self._osc_script_loaded = False
        if osc_style == "jellyfin":
            scripts.append(get_resource("trickplay-jf-osc.lua"))
        elif osc_style == "mpv":
            scripts.append(get_resource("trickplay-osc.lua"))
        if osc_style in ("jellyfin", "mpv"):
            self._osc_script_loaded = True
            mpv_options["osc"] = False
        elif osc_style == "mpvtk":
            # The in-window playback HUD replaces any OSC.
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
            mpv_options["tls_cert_file"] = settings.tls_client_cert
            mpv_options["tls_key_file"] = settings.tls_client_key

            if settings.tls_server_ca:
                mpv_options["tls_ca_file"] = settings.tls_server_ca

        # Audio-only files (music) are controlled from the browser's now-playing
        # bar, not an mpv window: don't decode embedded cover art into a video
        # track (which would otherwise pop a window showing the album art).
        # Only affects audio-only files — video and music videos are untouched.
        mpv_options["audio_display"] = "no"

        self._player = mpv.MPV(
            input_default_bindings=True,
            input_vo_keyboard=True,
            input_media_keys=settings.media_keys,
            log_handler=mpv_log_handler,
            loglevel=settings.mpv_log_level,
            **mpv_options,
        )

        # The menu object must survive mpv re-creation (crash recovery,
        # idle-quit): its is_menu_shown state gates idle_quit, and external
        # holders (the systray "Application Menu" entry) call into it. A fresh
        # OSDMenu here used to reset is_menu_shown to False mid-show, letting
        # idle_quit kill the window while the user was looking at the menu.
        if self.menu is None:
            self.menu = OSDMenu(self, self._player)
        else:
            self.menu.update_player(self._player)
        self.syncplay = SyncPlayManager(self)

        if discord_presence:
            try:
                register_join_event(self.syncplay.discord_join_group)
            except Exception:
                log.error("Could not register Discord join callback.", exc_info=True)

        if hasattr(self._player, "osc"):
            # Ensure the built-in OSC stays disabled when a shim OSC script
            # is loaded, even if the user's mpv.conf has osc=yes.
            if self._osc_script_loaded:
                self._player.osc = False
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

        @keypress(settings.kb_stop)
        def handle_kb_stop():
            # With the in-window browser, the window IS the library: q should
            # stop playback and drop back to browsing, not tear mpv down.
            # Closing the window (CLOSE_WIN) still quits.
            log.info("handle_stop triggered")
            if self.mpvtk_active:
                self.put_task(self.stop_to_browser)
            else:
                self.put_task(self.stop_and_close)

        @self._player.on_key_press("STOP")
        def handle_stop():
            log.info("handle_stop triggered")
            self.put_task(self.stop_and_close)

        @self._player.on_key_press("CLOSE_WIN")
        def handle_close_win():
            # With the in-window browser, closing the window is "minimize to
            # tray", not "quit" — but only the UI knows whether a tray is
            # actually there to minimize into, so it decides. Without that
            # hook this used to stop playback, which fired a stopped
            # playstate, which re-opened the browser window immediately.
            log.info("handle_close_win triggered")
            handler = self.on_window_closed
            if self.mpvtk_active and handler is not None:
                self.put_task(handler)
            else:
                self.put_task(self.stop_and_close)

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
                if self.is_in_intro and settings.skip_intro_on_seek:
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
            if self.do_not_handle_pause:
                self._player.show_text(_("Please wait, loading..."), 1000, 1)
                return
            if not self.menu.is_menu_shown:
                self.menu.show_menu()
            else:
                self.menu.hide_menu()

        @keypress(settings.kb_menu_esc)
        def menu_back():
            if self.menu.is_menu_shown:
                self.menu.menu_action("back")
            elif self._nav_back():
                pass    # the in-window UI consumed it (dialog / go back)
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
                if self.is_in_intro and settings.skip_intro_on_seek:
                    self.skip_intro()
                else:
                    self.kb_seek("right")

        @keypress(settings.kb_menu_up)
        def menu_up():
            if self.menu.is_menu_shown:
                self.menu.menu_action("up")
            else:
                if self.is_in_intro and settings.skip_intro_on_seek:
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
            # Only act on the True transition: the False transition means a
            # new file just loaded, and arming the pause-swallow there leaves
            # a stale "expect pause" that eats the user's first real pause
            # under SyncPlay.
            if self._video and reached_end:
                # Genuine end-of-file (as opposed to the playback-abort path,
                # which also fires on decode/network failure).
                self._reached_eof = True
                self._queue_finished()

        # Fires at the end.
        @self._player.property_observer("playback-abort")
        def handle_end_idle(_name, value: bool):
            if self._video and value and not self._video.parent.has_next:
                self._queue_finished()

        @self._player.property_observer("seeking")
        def handle_seeking(_name, value: bool):
            if self.do_not_handle_pause:
                return

            # Handle intro skip for any forward seek (including custom key bindings)
            if value:
                # Seeking started - store current position
                self.playback_time_before_seek = self._player.playback_time
            else:
                # Seeking ended - check if we should skip intro. Seeks made
                # from the jellyfin OSC's own controls are exempt (it has an
                # explicit skip button; scrubbing must not warp to the end
                # of the intro), and the whole behavior is a setting.
                if (
                    settings.skip_intro_on_seek
                    and time.time() - self._last_ui_seek_time > 2.0
                    and self.is_in_intro
                    and self.playback_time_before_seek is not None
                    and self._player.playback_time is not None
                    and self._player.playback_time > self.playback_time_before_seek
                ):
                    self.skip_intro()

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

            # Forwarding a pause flip to SyncPlay is only meaningful while
            # something is actually playing; an idle/torn-down player can
            # still emit pause events (external mpv, scripts).
            if value != self.pause_ignore and self._video:
                if self.syncplay.is_enabled():
                    if value:
                        self.syncplay.pause_request()
                    else:
                        # Don't allow unpausing locally through MPV.
                        self.syncplay.play_request()
                        self.set_paused(True, True)

        @self._player.event_callback("shutdown")
        def handle_shutdown(event):
            # We quit mpv ourselves to save resources — idle_quit already tore
            # down and there is no session to report. Don't run the stop hook
            # or re-terminate.
            if self._idle_quit:
                return
            log.info("mpv shutdown event received")
            # Only flip the flag here; the real teardown does network I/O and
            # swaps self._video, neither of which belongs on MPV's event
            # thread (the swap races the timeline thread, and blocking this
            # thread stalls every other observer). The queued task runs on
            # the action thread under _lock, serialized against stop()/play().
            self.should_send_timeline = False
            self.put_task(self._handle_mpv_shutdown)
            # The next re-open joins this (see _teardown_player), so a cast
            # landing right after a user-close can't build the new instance
            # while this one is still tearing down.
            self._terminate_thread = Thread(
                target=self._terminate_mpv, args=(self._player,), daemon=True
            )
            self._terminate_thread.start()

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
                elif args[0] == "shim-jf-osc-action":
                    self.osc_bridge.handle_action(args[1:])
                elif args[0] == "shim-jf-osc-ui-seek":
                    # The OSC is about to seek from its own controls;
                    # exempt the next couple of seconds from seek-to-skip.
                    self._last_ui_seek_time = time.time()
                elif args[0] == "shim-close":
                    # The OSC's back/close button. With the in-window UI this
                    # means "yield to the library", not "close the window" —
                    # _set_force_window keeps it up either way.
                    log.info("Received shim-close message")
                    if self._video and not self._player.playback_abort:
                        self.put_task(self.stop_and_close)
                    else:
                        self.put_task(self.force_window, False)
            except Exception:
                log.warning("Error when processing client-message.", exc_info=True)

        self._showing_browse_bg = False
        if settings.browser_ui == "mpvtk" and settings.enable_gui:
            # One window is shared by the browser and playback and the user
            # sizes it to suit, so mpv must not resize it on their behalf.
            # Two separate properties do that, and both default to yes:
            #   keepaspect-window  - snaps the window to the file's aspect
            #   auto-window-resize - resizes the window to the video's size
            # Set once here so they survive idle-quit / crash re-creation.
            for prop in ("keepaspect_window", "auto_window_resize"):
                try:
                    setattr(self._player, prop, False)
                except Exception:
                    log.debug("%s unsupported by this mpv", prop,
                              exc_info=True)

        self._mpv_alive = True

        # Anything attached to the *previous* handle has to move over. Only on
        # a re-open: on first init there is nothing attached yet (the UI
        # attaches after the player is constructed).
        if reopen and self.on_mpv_recreated is not None:
            try:
                self.on_mpv_recreated()
            except Exception:
                log.error("on_mpv_recreated handler failed", exc_info=True)

    def _notify_mpv_gone(self):
        handler = self.on_mpv_gone
        if handler is None:
            return
        try:
            handler()
        except Exception:
            log.error("on_mpv_gone handler failed", exc_info=True)

    # End-of-playback choreography shared by the eof and abort observers:
    # arm the pause-swallow, take the dedup lock non-blockingly (whichever
    # observer fires first wins), and stamp the task with the playback epoch
    # so it no-ops if a new file starts before it runs.
    def _queue_finished(self):
        self.pause_ignore = True
        has_lock = self._finished_lock.acquire(False)
        self.put_task(self.finished_callback, has_lock, self._play_epoch)

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
        video = self._video
        if video is None:
            return
        _, intro = video.get_current_intro(self._player.playback_time)
        if intro is None:
            return

        if not self._player.playback_abort:
            self._player.command("seek", intro.end, "absolute")

        intro.has_triggered = True
        self.timeline_handle()
        self.is_in_intro = False
        self._last_intro_msg_time = time.time()

    @synchronous("_lock")
    def update(self):
        # Drain queued tasks first, and never let one abort the drain: this
        # loop is pumped by the action thread, and an exception escaping here
        # would kill that thread for the rest of the session. Tasks must also
        # run when MPV is already gone (e.g. the shutdown teardown task), so
        # this happens before anything touches the player.
        while not self.evt_queue.empty():
            func, args = self.evt_queue.get()
            try:
                func(*args)
            except _mpv_errors:
                self._handle_mpv_disconnect()
            except Exception:
                log.exception(
                    "Queued task %s failed.", getattr(func, "__name__", func)
                )
        prev_hud_skip = self._hud_skip
        try:
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

                # With the jellyfin OSC, "ask" mode shows a floating Skip
                # Intro/Credits button instead of the seek-to-skip OSD
                # text prompt. The mpvtk HUD renders its own button from
                # _hud_skip (visible while the HUD is summoned).
                jf_skip_button = self.osc_bridge.active()
                hud_skip_button = (
                    settings.osc_style == "mpvtk" and self.mpvtk_active
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
                            (
                                _("Skipped Credits")
                                if intro.type == "Outro"
                                else _("Skipped Intro")
                            ),
                            3000,
                            1,
                        )
                        self._last_intro_msg_time = time.time()

                    if jf_skip_button:
                        self.osc_bridge.update_skip_button(
                            intro if should_prompt and not should_skip else None
                        )
                    elif hud_skip_button:
                        self._hud_skip = (
                            intro if should_prompt and not should_skip
                            else None
                        )
                    elif (
                        not self.is_in_intro
                        and should_prompt
                        and time.time() - self._last_intro_msg_time > 3
                    ):
                        self._player.show_text(
                            (
                                _("Seek to Skip Credits")
                                if intro.type == "Outro"
                                else _("Seek to Skip Intro")
                            ),
                            3000,
                            1,
                        )
                        self._last_intro_msg_time = time.time()
                    self.is_in_intro = True
                else:
                    if jf_skip_button:
                        self.osc_bridge.update_skip_button(None)
                    self._hud_skip = None
                    self.is_in_intro = False
            else:
                self._hud_skip = None
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        if (self._hud_skip is None) != (prev_hud_skip is None):
            # A skippable segment just started/ended: push a playstate
            # now so the HUD's skip button (and the idle overlay) track
            # it within a pump instead of the 5s timeline cadence.
            self.push_playstate()

        try:
            if self._video and not self._player.playback_abort:
                if not self.is_paused():
                    self.last_update.restart()
        except _mpv_errors:
            self._handle_mpv_disconnect()

        # Poll rescue for a LOST end-of-file notification: the eof/abort
        # observers ride the same external-mpv IPC event pipeline whose
        # delivery loss forced wait_property to become poll-assisted; if the
        # eof event never arrives, auto-advance silently dies and the session
        # shows "playing" forever. This runs ~1/s while a video is loaded.
        # Dedup with the observer path needs no new state: _queue_finished's
        # non-blocking _finished_lock + the playback epoch already discard
        # duplicates, play() drops should_send_timeline before advancing, and
        # the start_time guard keeps a stale read just after an advance from
        # re-finishing the new file.
        try:
            video = self._video
            if (
                video is not None
                and self.should_send_timeline
                and time.time() - (self.start_time or 0) > 5
            ):
                try:
                    eof = self._player.eof_reached
                except _mpv_errors:
                    self._handle_mpv_disconnect()
                    return
                except Exception:
                    eof = None  # property unavailable / backend quirk
                if eof is True:
                    self._reached_eof = True
                    self._queue_finished()
                elif not video.parent.has_next:
                    # Last item: keep_open is off, so mpv idles at the end and
                    # eof-reached reads unavailable — mirror the abort observer.
                    try:
                        abort = self._player.playback_abort
                    except _mpv_errors:
                        self._handle_mpv_disconnect()
                        return
                    except Exception:
                        abort = False
                    if abort:
                        self._queue_finished()
        except Exception:
            log.exception("End-of-file poll rescue failed.")

    def play(
        self,
        video: "Video_type",
        offset: int = 0,
        no_initial_timeline: bool = False,
        is_initial_play: bool = False,
        apply_memory: bool = True,
    ):
        if video is None:
            # build_video returns None when fully offline with no downloaded
            # copy; never let that propagate into a crash here.
            log.error("PlayerManager::play called without a video")
            return
        self.should_send_timeline = False
        self.start_time = time.time()
        url = video.get_playback_url()
        if not url:
            log.error("PlayerManager::play no URL found")
            return

        self._play_media(video, url, offset, no_initial_timeline, is_initial_play,
                         apply_memory)

    @synchronous("_lock")
    def _play_media(
        self,
        video: "Video_type",
        url: str,
        offset: int = 0,
        no_initial_timeline: bool = False,
        is_initial_play: bool = False,
        apply_memory: bool = True,
    ):
        self._ensure_mpv()

        self.pause_ignore = True
        self.do_not_handle_pause = True
        self.url = url
        self._showing_browse_bg = False   # real media replaces the backdrop
        self.menu.hide_menu()

        if self.trickplay:
            self.trickplay.clear()

        if settings.log_decisions:
            log.info("Playing: {0}".format(url))
        if self.get_webview() is not None and settings.display_mirroring:
            # noinspection PyUnresolvedReferences
            self.get_webview().hide()

        # Expose the source path so external-mpv profiles can auto-apply (see 986ceae).
        # Use the real `set` input command, not `set_property`: the latter is a
        # JSON-IPC-only verb and crashes libmpv ("Command 'set_property' not found",
        # ValueError -4). Best-effort only; never let it break playback.
        try:
            self._player.command(
                "set", "user-data/media-source/Path", video.media_source.get("Path")
            )
        except Exception:
            log.debug("Could not set user-data/media-source/Path", exc_info=True)
        # Apply the persisted per-type volume BEFORE playback starts, so the
        # track never briefly blares at the default while mpv probes/loads
        # (duration isn't known yet, so use the item we're about to play).
        try:
            v_item = getattr(video, "item", None) or {}
            v_audio = (v_item.get("MediaType") == "Audio"
                       or v_item.get("Type") == "Audio")
            self._player.volume = (settings.music_volume if v_audio
                                   else settings.video_volume)
        except _mpv_errors:
            pass
        self._player.play(self.url)
        if not wait_property(
            self._player,
            "duration",
            lambda x: x is not None,
            settings.playback_timeout,
            skip_initial=True,
        ):
            # Playback attempt timed out (settings.playback_timeout seconds).
            log.error("Timeout when waiting for media duration. Stopping playback!")
            self.stop()
            return
        log.info("Finished waiting for media duration.")
        self._video = video
        # Music has no picture — going fullscreen for it just blanks the
        # screen (and, with the in-window browser, hides the library the
        # now-playing bar belongs to).
        if (settings.fullscreen and not self.fullscreen_disable
                and not self._current_is_audio()):
            self._player.fs = True
        self._player.force_media_title = video.get_proper_title()
        # A new file is actually playing now; any prior end-of-file is stale,
        # and so is the previous file's last known position (it would
        # otherwise satisfy the near-end finish check for a same-length next
        # episode that aborts before its first timeline tick).
        self._reached_eof = False
        self._last_playback_position = 0
        # Invalidate finished-callbacks queued for the previous playback: a
        # cast landing in the same instant as an EOF would otherwise let the
        # stale callback mark the just-cast item played and skip past it.
        self._play_epoch += 1
        self.is_in_intro = False
        self.external_subtitles = {}
        self.external_subtitles_rev = {}

        self.upd_player_hide()
        if is_initial_play:
            self._track_memory = None  # new queue; start fresh
        elif apply_memory and self._track_memory is not None:
            self._apply_remembered_tracks(video)
        self.configure_streams()
        self._capture_track_memory(video)
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

        self.osc_bridge.send_state()

        if self.syncplay.is_enabled():
            self.set_speed(1)
            self.syncplay.play_done()
        else:
            self.set_paused(False, False)

        # Trickplay (scrubbing thumbnails) is video-only — skip the fetch for
        # audio so switching songs isn't slowed by a pointless request.
        if self.trickplay and not v_audio:
            self.trickplay.fetch_thumbnails()

        self.should_send_timeline = True
        # Fresh offline-record throttle window for each newly playing item.
        self._last_offline_record = float("-inf")
        self.do_not_handle_pause = False
        # Repeat-one loops the current file, but only for audio — re-apply per
        # track so a video started while repeat="one" is held over never loops.
        # (Volume was already applied before play(); set_paused above already
        # pushed the now-playing state to the music bar.)
        try:
            self._player.loop_file = (
                "inf" if self.repeat_mode == "one" and self._current_is_audio()
                else "no")
        except _mpv_errors:
            pass
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

        if self.menu.is_menu_shown:
            self.menu.hide_menu()

        local_video = self._video
        if not local_video or not self._mpv_alive:
            self.exec_stop_cmd()
            return

        try:
            if self._player.playback_abort:
                self.exec_stop_cmd()
                return
        except _mpv_errors:
            self._handle_mpv_disconnect()
            self.exec_stop_cmd()
            return

        log.info("PlayerManager::stop stopping playback of %s" % local_video)

        self.should_send_timeline = False
        options = self.get_timeline_options(video=local_video)
        self.set_paused(False)
        self._video = None
        self._player.command("stop")
        local_video.terminate_transcode()
        if local_video.client is None and hasattr(local_video,
                                                  "record_offline_progress"):
            local_video.record_offline_progress(options.get("PositionTicks"))
        self.send_timeline_stopped(options=options, client=local_video.client)
        self.exec_stop_cmd()
        # Hide the browser's music bar now that nothing is playing.
        self.push_playstate(stopped=True)

        if self.trickplay:
            self.trickplay.clear()

    def _set_force_window(self, value):
        """Single authority for force_window.

        With the in-window UI there is one window and it must survive
        everything except being minimized: stopping playback, the OSC's close
        button, the end of a queue and closing the OSD menu all used to drop
        it, which showed as the window vanishing and being rebuilt. So while
        ``mpvtk_active`` (the browser owns the window — true even while
        yielded to playback, false only once minimized) force_window never
        goes False. The minimize path clears mpvtk_active first, which is
        what lets it through."""
        if not value and self.mpvtk_active:
            log.debug("force_window=False suppressed: the in-window UI "
                      "owns this window")
            return
        self._player.force_window = value

    def stop_and_close(self):
        log.info("stop_and_close: stopping playback")
        self.stop()
        if not self._mpv_alive:
            return
        try:
            self._player.keep_open = False
            self._set_force_window(False)
            self._player.command("stop")
        except _mpv_errors:
            self._handle_mpv_disconnect()
        log.info("stop_and_close: done")

    def stop_to_browser(self):
        """Stop playback but keep the window, so the in-window browser can take
        it back (the 'q' key while browser_ui=mpvtk). push_playstate(stopped)
        is what tells the browser to re-enter browse mode."""
        log.info("stop_to_browser: stopping playback, keeping the window")
        self.stop()
        if not self._mpv_alive:
            return
        self.set_browse_window(True)

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
                    p2 = "absolute"
                    if exact:
                        p2 += "+exact"
                    self._player.command("seek", offset, p2)
                else:
                    if self.syncplay.is_enabled():
                        self.last_seek = self._player.playback_time + offset
                    if exact:
                        self._player.command("seek", offset, "exact")
                    else:
                        self._player.command("seek", offset)
        self.timeline_handle()
        self.push_playstate()

    @synchronous("_lock")
    def set_volume(self, pct: float):
        if not self._player.playback_abort:
            self._player.volume = pct
        self.timeline_handle()
        self.push_playstate()

    @synchronous("_lock")
    def get_state(self):
        if self._player.playback_abort:
            return "stopped"

        if self._player.pause:
            return "paused"

        return "playing"

    @synchronous("_lock")
    def is_paused(self):
        try:
            if not self._player.playback_abort:
                return self._player.pause
        except _mpv_errors:
            self._handle_mpv_disconnect()
        return False

    @synchronous("_lock")
    def finished_callback(self, has_lock: bool, epoch: Optional[int] = None):
        # Queued for an earlier playback? A new file has started since this
        # task was enqueued; acting now would finish the wrong video. The
        # _finished_lock needs no release here — _play_media already released
        # it when it bumped the epoch.
        if epoch is not None and epoch != self._play_epoch:
            log.info("PlayerManager::finished_callback stale, skipping")
            return

        # Snapshot: an mpv disconnect on another thread can null self._video
        # mid-callback even though we hold _lock.
        video = self._video
        if not video:
            self.pause_ignore = False
            return

        # Only mark played on a genuine end-of-file. An errored/aborted stream
        # (playback-abort far from the end) must not be recorded as watched.
        if settings.force_set_played and self._finished_at_eof(video):
            video.set_played()
        # Repeat-all wraps back to the first track when the queue runs out
        # (repeat-one loops in mpv and never reaches here). SyncPlay drives its
        # own advance, so wrap only applies to normal local playback.
        wrap = (self.repeat_mode == "all" and self._current_is_audio()
                and not video.parent.has_next
                and not self.syncplay.is_enabled()
                and len(video.parent.queue) > 0)
        if (video.parent.has_next or wrap) and settings.auto_play:
            if has_lock:
                log.info("PlayerManager::finished_callback starting next episode")
                if wrap:
                    first = video.parent.get_from_key(
                        video.parent.queue[0]["Id"])
                    new_video = first.video if first else None
                else:
                    new_video = video.parent.get_next().video
                self.send_timeline_stopped(True)
                if new_video is None:
                    # Offline and the next episode isn't downloaded: end the
                    # session gracefully instead of crashing auto-advance.
                    log.warning("Next item is not available offline; stopping.")
                    self.show_text(_("Next episode is not downloaded."), 5000, 1)
                elif self.syncplay.is_enabled():
                    self.syncplay.request_next(video.get_playlist_id())
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
            # The queue is done — drop the finished video and unload it.
            # Leaving _video set kept the app looking "active", so once the
            # browser re-loaded its background image (which clears
            # playback-abort) the next timeline tick reported the *finished*
            # item as playing again and the UI bounced back to the player,
            # showing the ended video paused.
            self.should_send_timeline = False
            self._video = None
            try:
                video.terminate_transcode()
            except Exception:
                log.debug("terminate_transcode failed at end of queue",
                          exc_info=True)
            try:
                self._player.command("stop")
            except _mpv_errors:
                self._handle_mpv_disconnect()
            self.push_playstate(stopped=True)
        self.pause_ignore = False

    @synchronous("_lock")
    def watched_skip(self):
        if not self._video:
            return

        # Advance (which sends the final stop report at the current position)
        # BEFORE marking played: the other order let the stop report land
        # after set_played and overwrite the fully-watched state with
        # mid-episode progress. unwatched_quit uses the same stop-then-mark
        # order for the same reason. finally: the user's explicit mark must
        # not be lost just because the advance failed (e.g. the next item's
        # playback-info errored).
        video = self._video
        try:
            self.play_next()
        finally:
            video.set_played()

    @synchronous("_lock")
    def unwatched_quit(self):
        if not self._video:
            return

        video = self._video
        self.stop_and_close()
        video.set_played(False)

    @synchronous("_lock")
    def play_next(self):
        video = self._video
        if video and video.parent.has_next:
            new_video = video.parent.get_next().video
            self.send_timeline_stopped(True)
            if self.syncplay.is_enabled():
                self.syncplay.request_next(video.get_playlist_id())
            else:
                self.play(new_video)
            return True
        return False

    @synchronous("_lock")
    def skip_to(self, key: str):
        video = self._video
        media = video.parent.get_from_key(key) if video else None
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
        video = self._video
        if video and video.parent.has_prev:
            new_video = video.parent.get_prev().video
            self.send_timeline_stopped(True)
            if self.syncplay.is_enabled():
                self.syncplay.request_prev(video.get_playlist_id())
            else:
                self.play(new_video)
            return True
        return False

    @synchronous("_lock")
    def get_queue_ids(self):
        """The currently-playing queue's item ids (for 'add queue to playlist')."""
        video = self._video
        if video is None:
            return []
        return [q.get("Id") for q in video.parent.queue if q.get("Id")]

    @synchronous("_lock")
    def get_queue(self):
        """The full queue for the browser's queue display: each entry's item id
        + PlaylistItemId, plus which one is playing."""
        video = self._video
        if video is None:
            return {"items": [], "current_id": None}
        return {
            "items": [{"id": q.get("Id"),
                       "playlist_item_id": q.get("PlaylistItemId")}
                      for q in video.parent.queue if q.get("Id")],
            "current_id": video.item_id,
        }

    def _publish_queue(self, m, new_queue, current_pid):
        """Publish a rebuilt queue and re-point seq/has_next/has_prev at the
        still-playing track. Never mutate the queue in place — the finished
        callback reads queue/seq/has_next lock-free on other threads."""
        m.queue = new_queue  # atomic publish first
        m.seq = next((i for i, q in enumerate(new_queue)
                      if q.get("PlaylistItemId") == current_pid), 0)
        m.has_next = m.seq < len(new_queue) - 1
        m.has_prev = m.seq > 0

    @synchronous("_lock")
    def queue_remove_many(self, playlist_item_ids):
        """Drop the given queue entries (never the currently-playing one)."""
        video = self._video
        if video is None:
            return False
        m = video.parent
        current_pid = m.queue[m.seq].get("PlaylistItemId")
        drop = set(playlist_item_ids) - {current_pid}
        if not drop:
            return False
        new_queue = [q for q in m.queue
                     if q.get("PlaylistItemId") not in drop]
        self._publish_queue(m, new_queue, current_pid)
        return True

    @synchronous("_lock")
    def queue_reorder(self, ordered_playlist_item_ids):
        """Rebuild the queue to match the given PlaylistItemId order (the
        browser computes it for block Top/Up/Down/Bottom moves), keeping seq on
        the still-playing track. Any entry the browser didn't list is appended
        so the queue can never lose tracks."""
        video = self._video
        if video is None:
            return False
        m = video.parent
        by_pid = {q.get("PlaylistItemId"): q for q in m.queue}
        listed = set(ordered_playlist_item_ids)
        new_queue = [by_pid[p] for p in ordered_playlist_item_ids
                     if p in by_pid]
        new_queue += [q for q in m.queue
                      if q.get("PlaylistItemId") not in listed]
        if not new_queue:
            return False
        current_pid = m.queue[m.seq].get("PlaylistItemId")
        self._publish_queue(m, new_queue, current_pid)
        return True

    @synchronous("_lock")
    def try_skip_within_queue(self, item_ids, start_index):
        """Fast path for clicking another track in the CURRENTLY-PLAYING queue:
        seek within the existing queue instead of rebuilding it (and re-opening
        a whole new play session for the same list). Returns True if handled,
        False to fall back to a normal start_playback."""
        video = self._video
        if video is None:
            return False
        try:
            if self._player.playback_abort:
                return False
        except _mpv_errors:
            return False
        queue = video.parent.queue
        if [q.get("Id") for q in queue] != list(item_ids):
            return False
        if not 0 <= start_index < len(queue):
            return False
        target_id = queue[start_index].get("Id")
        if target_id == video.item_id:
            return True  # already playing that track — nothing to do
        return bool(self.skip_to(target_id))

    @synchronous("_lock")
    def set_repeat(self, mode):
        """Repeat mode for the music bar: 'none' | 'all' | 'one'. 'one' loops
        the current file in mpv; 'all' wraps the queue at the end (handled in
        finished_callback); 'none' is the default. Repeat is a MUSIC feature:
        loop-file is applied only while audio plays (and re-applied per track in
        _play_media) so it never makes a video loop."""
        if mode not in ("none", "all", "one"):
            return
        self.repeat_mode = mode
        try:
            self._player.loop_file = (
                "inf" if mode == "one" and self._current_is_audio() else "no")
        except _mpv_errors:
            self._handle_mpv_disconnect()
        self.push_playstate()

    @synchronous("_lock")
    def toggle_current_favorite(self):
        """Flip the now-playing item's favorite state (music bar heart)."""
        video = self._video
        if video is None or video.client is None:
            return
        item = getattr(video, "item", None)
        if item is None:
            return
        ud = item.setdefault("UserData", {})
        new_state = not ud.get("IsFavorite")
        try:
            video.client.jellyfin.favorite(video.item_id, new_state)
            ud["IsFavorite"] = new_state
        except Exception:
            log.error("Failed to toggle favorite for %s", video.item_id,
                      exc_info=True)
        self.push_playstate()

    @synchronous("_lock")
    def restart_playback(self):
        video = self._video
        if not video:
            return False
        current_time = self._player.playback_time
        # Same item, same media source: the video already carries the user's
        # exact aid/sid (e.g. a just-selected burn-in subtitle). Don't re-derive
        # tracks from memory or we'd revert the very change that forced this
        # restart.
        self.play(video, current_time, apply_memory=False)
        return True

    @synchronous("_lock")
    def get_video_attr(self, attr: str, default=None):
        if self._video:
            return self._video.get_video_attr(attr, default)
        return default

    def _capture_track_memory(self, video):
        self._track_memory = ((video.media_source or {}), video.aid, video.sid)

    def _apply_remembered_tracks(self, video):
        """Carry the previous episode's audio/subtitle choice into this one,
        matching by language/title/codec/position (jellyfin-web heuristic)."""
        prev_source, prev_aid, prev_sid = self._track_memory
        streams = (video.media_source or {}).get("MediaStreams") or []

        if settings.remember_audio_track and prev_aid is not None:
            match = _rank_stream(prev_source, prev_aid, streams, "Audio")
            if match is not None:
                video.aid = match

        if settings.remember_subtitle_track:
            if prev_sid is None or prev_sid == -1:
                video.sid = -1  # subtitles were off — keep them off
            else:
                match = _rank_stream(prev_source, prev_sid, streams, "Subtitle")
                if match is not None:
                    video.sid = match

    @synchronous("_lock")
    def configure_streams(self):
        video = self._video
        if not video:
            return
        audio_uid = video.aid
        sub_uid = video.sid

        if audio_uid is not None and not video.is_transcode:
            log.info("PlayerManager::play selecting audio stream index=%s" % audio_uid)
            self._player.audio = video.audio_seq[audio_uid]

        if sub_uid is None or sub_uid == -1:
            log.info("PlayerManager::play selecting subtitle stream (none)")
            self._player.sub = "no"
        else:
            log.info("PlayerManager::play selecting subtitle stream index=%s "
                     "(embedded map=%s external=%s)" % (
                         sub_uid, video.subtitle_seq,
                         list(video.subtitle_url)))
            if sub_uid in video.subtitle_seq:
                self._player.sub = video.subtitle_seq[sub_uid]
            elif sub_uid in video.subtitle_url:
                log.info(
                    "PlayerManager::play selecting external subtitle id=%s" % sub_uid
                )
                self.load_external_sub(sub_uid)
            else:
                log.warning("PlayerManager::subtitle index %s not in embedded or "
                            "external maps; leaving current selection.", sub_uid)

    @synchronous("_lock")
    def set_streams(self, audio_uid: int, sub_uid: int):
        video = self._video
        if not video:
            return
        need_restart = video.set_streams(audio_uid, sub_uid)

        if need_restart:
            self.restart_playback()
        else:
            self.configure_streams()
        # Remember the user's manual choice for subsequent episodes.
        self._capture_track_memory(self._video)
        # Keep the jellyfin OSC's menus in sync no matter who changed the
        # tracks (OSC itself, the c menu, or a remote client).
        self.osc_bridge.send_state()
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
        self.set_fullscreen(not self._player.fs, persist=True)

    @synchronous("_lock")
    def set_fullscreen(self, enabled: bool, persist: bool = False):
        """``persist`` remembers the choice, for toggles the *user* made (a
        key, the OSC button, a remote command) as opposed to ones the app
        makes for its own reasons — the update notice dropping out of
        fullscreen, or the browser opening windowed.

        Which key it lands in depends on what's on screen: browsing writes
        browser_fullscreen, playback writes fullscreen. They're separate
        settings precisely because people want different answers for the
        two."""
        self._player.fs = enabled
        self.fullscreen_disable = not enabled
        if not persist:
            return
        key = "fullscreen" if self._video is not None else "browser_fullscreen"
        if getattr(settings, key) == enabled:
            return
        setattr(settings, key, enabled)
        try:
            settings.save()
        except Exception:
            log.error("Could not persist %s", key, exc_info=True)

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
        self.push_playstate()

    @synchronous("_lock")
    def script_message(self, command, *args):
        if not self._mpv_alive:
            return
        try:
            self._player.command("script-message", command, *args)
        except _mpv_errors:
            self._handle_mpv_disconnect()

    def get_track_ids(self):
        return self._video.aid, self._video.sid

    def update_subtitle_visuals(self):
        self._player.sub_pos = SUBTITLE_POS[settings.subtitle_position]
        self._player.sub_scale = settings.subtitle_size / 100
        self._player.sub_color = settings.subtitle_color
        self.timeline_handle()

    def _current_is_audio(self):
        video = self._video
        if video is None:
            return False
        item = getattr(video, "item", None) or {}
        return item.get("MediaType") == "Audio" or item.get("Type") == "Audio"

    def _maybe_save_volume(self):
        """Persist the current volume into its per-type bucket if it changed.
        Called from the timeline tick (off mpv's event thread), so a volume
        change made via the music bar OR mpv keys survives a restart without
        hammering the settings file."""
        if self._video is None:
            return
        try:
            vol = int(self._player.volume)
        except (_mpv_errors, TypeError):
            return
        key = "music_volume" if self._current_is_audio() else "video_volume"
        if getattr(settings, key) != vol:
            setattr(settings, key, vol)
            settings.save()

    def push_playstate(self, stopped=False):
        """Feed the browser's now-playing bar a compact snapshot on each
        playback state change. Never raises and never re-enters MPV's lock — a
        bar refresh must never disturb playback. A ``stopped`` payload tells the
        bar to hide."""
        cb = self.on_playstate
        if cb is None:
            return
        try:
            video = self._video
            try:
                aborted = self._player.playback_abort
            except _mpv_errors:
                aborted = True
            if stopped or video is None or aborted:
                cb({"stopped": True})
                return
            item = getattr(video, "item", None) or {}
            try:
                pos = self._player.playback_time
                duration = self._player.duration
                paused = self._player.pause
                volume = self._player.volume
                muted = self._player.mute
                fullscreen = getattr(self._player, "fullscreen", None)
            except _mpv_errors:
                cb({"stopped": True})
                return
            skip = self._hud_skip
            cb({
                "stopped": False,
                "is_audio": (item.get("MediaType") == "Audio"
                             or item.get("Type") == "Audio"),
                "skip_label": (
                    (_("Skip Credits") if skip.type == "Outro"
                     else _("Skip Intro"))
                    if skip is not None else None
                ),
                "title": item.get("Name") or "",
                "artist": ", ".join(item.get("Artists") or []),
                "album": item.get("Album") or "",
                "position": float(pos) if pos is not None else 0.0,
                "duration": (float(duration) if duration is not None
                             else float(video.get_duration() or 0.0)),
                "paused": bool(paused),
                "volume": int(volume) if volume is not None else 100,
                "muted": bool(muted),
                "favorite": bool((item.get("UserData") or {}).get("IsFavorite")),
                "repeat": self.repeat_mode,
                "fullscreen": bool(fullscreen),
            })
        except Exception:
            log.debug("push_playstate failed", exc_info=True)

    def get_timeline_options(self, finished=False, video=None):
        # PlaylistItemId is dynamically generated. A more stable Id will be used
        # if queue manipulation is added as a feature.
        # self._video can be nulled at any moment by another thread (stop,
        # mpv disconnect) — take one snapshot and use only the local from
        # here on. Callers must handle a None return.
        if video is None:
            video = self._video
        if video is None:
            return None
        player = self._player

        # Cache player properties to reduce IPC calls (especially with external
        # MPV). Tolerate MPV being mid-shutdown — closing via the OSC 'x'
        # button can race the final timeline send and would otherwise crash.
        try:
            volume = player.volume
            mute = player.mute
            pause = player.pause
            duration = player.duration
            cache_buffering = player.cache_buffering_state
            playback_time = player.playback_time
        except _mpv_errors:
            volume = mute = pause = duration = cache_buffering = playback_time = None

        if playback_time is not None:
            self._last_playback_position = playback_time

        if finished and self._finished_at_eof(video, playback_time):
            # Genuine end-of-file: report the full duration so the item is
            # recorded as fully watched.
            safe_pos = video.get_duration() or 0
        elif finished:
            # "Finished" without a real EOF means an abort (decode/network
            # failure, or mpv already exited). Don't pretend it was watched to
            # the end — fall back to the last known position.
            if playback_time is None:
                safe_pos = self._last_playback_position
            else:
                safe_pos = playback_time
        else:
            safe_pos = playback_time or 0
        self.last_seek = safe_pos
        self.pause_ignore = pause
        options = {
            "VolumeLevel": int(none_fallback(volume, 100)),
            "IsMuted": mute,
            "IsPaused": pause,
            "RepeatMode": {"all": "RepeatAll", "one": "RepeatOne"}.get(
                self.repeat_mode, "RepeatNone"),
            # "MaxStreamingBitrate": 140000000,
            "PositionTicks": int(safe_pos * 10000000),
            "PlaybackStartTimeTicks": int(self.start_time * 10000000),
            "SubtitleStreamIndex": none_fallback(video.sid, -1),
            "AudioStreamIndex": none_fallback(video.aid, -1),
            "BufferedRanges": [],
            "PlayMethod": "Transcode" if video.is_transcode else "DirectPlay",
            "PlaySessionId": video.playback_info["PlaySessionId"],
            "PlaylistItemId": video.get_playlist_id(),
            "MediaSourceId": video.media_source["Id"],
            "CanSeek": True,
            "ItemId": video.item_id,
            "NowPlayingQueue": video.parent.queue,
        }
        if duration is not None:
            options["BufferedRanges"] = [
                {
                    "start": int(safe_pos * 10000000),
                    "end": int(
                        (
                            (
                                duration
                                - safe_pos * none_fallback(cache_buffering, 0) / 100
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
                    video.is_tv
                    and video.item.get("IndexNumber") is not None
                    and video.item.get("ParentIndexNumber") is not None
                ):
                    title = video.item.get("SeriesName")
                    subtitle = _("Season {0} - Episode {1}").format(
                        video.item.get("ParentIndexNumber"),
                        video.item.get("IndexNumber"),
                    )
                else:
                    title = video.item.get("Name")
                    subtitle = str(video.item.get("ProductionYear", ""))
                send_presence(
                    title,
                    subtitle,
                    playback_time,
                    duration,
                    not pause,
                    self.syncplay.current_group,
                    video.item.get("Type"),
                )
            except Exception:
                log.error("Could not send Discord Rich Presence.", exc_info=True)
        return options

    @synchronous("_tl_lock")
    def send_timeline(self):
        video = self._video
        try:
            if (
                self.should_send_timeline
                and video
                and not self._player.playback_abort
            ):
                if video.client is not None:
                    # Hold progress until the (async) session_playing has opened
                    # the session, so a session_progress can't arrive first.
                    if not self._session_ready.is_set():
                        return
                    options = self.get_timeline_options(video=video)
                    if options is not None:
                        video.client.jellyfin.session_progress(options)
                    try:
                        if self.syncplay.is_enabled():
                            self.syncplay.sync_playback_time()
                    except:
                        log.error("Error syncing playback time.", exc_info=True)
                elif hasattr(video, "record_offline_progress"):
                    # Offline playback has no server session, so stop() is the
                    # only other place the resume position is saved — an
                    # unclean exit (crash/power-off) would lose it. Persist it
                    # periodically here instead. Throttle so we don't hammer
                    # SQLite every 5s tick.
                    now = time.monotonic()
                    if now - self._last_offline_record >= 30:
                        options = self.get_timeline_options(video=video)
                        if options is not None:
                            self._last_offline_record = now
                            video.record_offline_progress(
                                options.get("PositionTicks"))
        except _mpv_errors:
            log.warning("MPV connection lost during timeline update.")
            self._handle_mpv_disconnect()

    @synchronous("_tl_lock")
    def _session_playing_safe(self, client, options):
        try:
            client.jellyfin.session_playing(options)
        except Exception:
            log.debug("session_playing failed", exc_info=True)
        finally:
            # Progress reports are gated on this — never leave it clear, even on
            # error, or timeline updates would stall for the whole session.
            self._session_ready.set()

    def send_timeline_initial(self):
        video = self._video
        if video is None or video.client is None:
            self._session_ready.set()
            return  # gone, or offline playback: no server session to open
        options = self.get_timeline_options(video=video)
        if options is None:
            self._session_ready.set()
            return
        # Open the session off the play path (a remote round-trip that would
        # otherwise delay switching tracks), but gate progress reports until it
        # completes so a session_progress can't race ahead of session_playing.
        self._session_ready.clear()
        Thread(target=self._session_playing_safe,
               args=(video.client, options), daemon=True).start()

    @synchronous("_tl_lock")
    def send_timeline_stopped(self, finished=False, options=None, client=None):
        self.should_send_timeline = False

        video = self._video
        if options is None:
            options = self.get_timeline_options(finished, video=video)

        # Capture offline progress for the auto-advance / finish paths (stop()
        # handles the explicit-stop case before clearing self._video).
        if client is None and video is not None and video.client is None \
                and options is not None \
                and hasattr(video, "record_offline_progress"):
            video.record_offline_progress(
                options.get("PositionTicks"), finished)

        if client is None:
            client = video.client if video else None

        # If the video vanished under us (mpv shutdown/disconnect on another
        # thread), the stop report has been or will be sent by whoever tore it
        # down; a client of None means offline playback (no server session).
        # Either way, still run the local cleanup below.
        if options is not None and client is not None:
            client.jellyfin.session_stop(options)

        if self.get_webview() is not None and settings.display_mirroring:
            self.get_webview().show()

        if discord_presence:
            try:
                clear_presence()
            except Exception:
                log.error("Could not clear Discord Rich Presence.", exc_info=True)

    def upd_player_hide(self):
        video = self._video
        if video:
            self._player.keep_open = video.parent.has_next

    def _finished_at_eof(self, video, playback_time=None):
        """Whether the playback that just ended genuinely reached the end.

        eof-reached only fires while keep_open holds the finished file, and
        keep_open is only set when there is a next item — so the last item in
        a queue ends via playback-abort alone. Accept a last known position
        at/near the duration as a genuine finish too; a mid-file decode or
        network abort stays far from the end and is not counted. The margin
        (95% or within 10s) absorbs the timeline tick interval and metadata
        duration drift."""
        if self._reached_eof:
            return True
        duration = video.get_duration()
        if not duration:
            return False
        position = max(playback_time or 0, self._last_playback_position)
        return position >= duration * 0.95 or duration - position <= 10

    def _ensure_mpv(self):
        """Re-create the mpv process if it is not running — closed by the user,
        crashed, or quit while idle (mpv_idle_quit). Called by the play path so
        a cast/remote Play transparently re-opens a fresh window. There is no
        local input while the window is gone, so play() is the only re-open
        trigger."""
        if not self._mpv_alive:
            log.info("mpv is not running; reinitializing.")
            self._idle_quit = False
            self._init_mpv()

    @synchronous("_lock")
    def idle_quit(self):
        """Quit mpv while idle to free the window / GPU context / memory
        (opt-in via mpv_idle_quit). Re-created on the next play. Gated hard so
        it never fires while anything still needs the window."""
        if not self._mpv_alive or self._video is not None:
            return
        if self.menu.is_menu_shown or self.syncplay.is_enabled():
            return
        if self.get_webview() is not None:
            return
        if self.mpvtk_active:
            # The in-window browser is on screen; keep the window alive. Note
            # this is cleared when the browser minimizes, so a minimized app
            # *does* idle-quit — which is most of the point of minimizing.
            return
        if is_using_ext_mpv and not settings.mpv_ext_start:
            # Never kill an mpv the user launched themselves.
            return
        log.info("Idle timeout reached; quitting mpv to save resources.")
        self._idle_quit = True
        self.should_send_timeline = False
        player = self._player
        self._teardown_player()
        self._mpv_alive = False
        self._terminate_thread = Thread(
            target=self._terminate_mpv, args=(player,), daemon=True
        )
        self._terminate_thread.start()
        # The handle is gone: let attached UIs drop anything keyed to it.
        # For the in-window browser that's the composited tile bitmaps, which
        # on libmpv are in-process buffers mpv reads by address — keeping them
        # would defeat the whole point of quitting to save memory.
        self._notify_mpv_gone()

    def _handle_mpv_disconnect(self):
        if not self._mpv_alive:
            return
        log.info("MPV connection lost, marking as dead for reconnect on next play.")
        self._mpv_alive = False
        self.should_send_timeline = False
        video = self._video
        self._video = None
        # If we spawned this (now unresponsive) mpv, make sure it's gone —
        # otherwise the next play() starts a second instance on top of a
        # possibly still-running one. The next re-open joins this thread (see
        # _teardown_player) so the new instance isn't built concurrently.
        self._terminate_thread = Thread(
            target=self._terminate_mpv, args=(self._player,), daemon=True
        )
        self._terminate_thread.start()
        self._notify_mpv_gone()
        if video:
            # The server still thinks we're playing; report the stop with the
            # last known position so the session and any transcode are freed.
            Thread(
                target=self._report_stopped_offline, args=(video,), daemon=True
            ).start()

    # Queued from the mpv "shutdown" event; runs on the action thread under
    # _lock so the _video swap can't race stop()/play(). The network report
    # happens off-thread — holding _lock for an HTTP timeout would freeze
    # casts and key handling.
    def _handle_mpv_shutdown(self):
        video = self._video
        if video:
            self._video = None
            Thread(
                target=self._report_stopped_offline, args=(video,), daemon=True
            ).start()
        self.exec_stop_cmd()

    # Best-effort stop report for a video whose mpv is already gone; options
    # are built from bookkeeping, not player properties. Routed through
    # send_timeline_stopped so the webview and Discord presence cleanup run
    # like any other stop.
    def _report_stopped_offline(self, video):
        options = {
            "PositionTicks": int((self.last_seek or 0) * 10000000),
            "PlaybackStartTimeTicks": int((self.start_time or 0) * 10000000),
            "PlayMethod": "Transcode" if video.is_transcode else "DirectPlay",
            "PlaySessionId": video.playback_info["PlaySessionId"],
            "ItemId": video.item_id,
        }
        # Offline playback has no server session; keep the position locally so
        # closing the mpv window doesn't lose it.
        if video.client is None and hasattr(video, "record_offline_progress"):
            try:
                video.record_offline_progress(options.get("PositionTicks"), False)
            except Exception:
                log.warning("Could not record offline progress.", exc_info=True)
        try:
            self.send_timeline_stopped(options=options, client=video.client)
        except Exception:
            log.warning("Could not report playback stop to server.", exc_info=True)
        try:
            video.terminate_transcode()
        except Exception:
            pass

    def _terminate_mpv(self, player=None):
        log.info("Terminating mpv instance")
        if player is None:
            player = self._player
        # Only mark dead if this is still the current instance. A terminate of
        # a superseded player that finishes after a re-open must not flip the
        # freshly-created player to dead.
        if player is self._player:
            self._mpv_alive = False
        try:
            player.terminate()
        except Exception:
            log.debug("Error terminating mpv", exc_info=True)
        log.info("mpv instance terminated")

    def terminate(self):
        self.stop()
        if is_using_ext_mpv:
            self._player.terminate()

        if self.trickplay:
            self.trickplay.stop()

    def get_seek_times(self):
        if self._jf_settings is None:
            if self._video.client is None:
                return -15.0, 30.0  # offline: server prefs unavailable, use defaults
            self._jf_settings = self._video.client.jellyfin.get_user_settings()
        custom_prefs = self._jf_settings.get("CustomPrefs") or {}
        seek_left = custom_prefs.get("skipBackLength") or 15000
        seek_right = custom_prefs.get("skipForwardLength") or 30000
        return -int(seek_left) / 1000, int(seek_right) / 1000

    # Wrappers to avoid private access
    def is_active(self):
        return bool(self._player and self._video)

    def is_playing(self):
        try:
            return bool(self._video and not self._player.playback_abort)
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return False

    def is_not_paused(self):
        try:
            return bool(
                self._video
                and not self._player.playback_abort
                and not self._player.pause
            )
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return False

    def has_video(self):
        return self._video is not None

    def get_video(self):
        return self._video

    def get_mpv(self):
        """The raw mpv handle, so the in-window UI (mpvtk) can attach to
        the same window used for playback instead of opening its own.
        Pair with the module-level ``is_using_ext_mpv`` flag, which tells
        the UI whether it's an external jsonipc process or in-process
        libmpv. See mpvtk.app.MpvtkApp.attach."""
        return self._player

    def show_text(self, text: str, duration: int, level: int = 1):
        if not self._mpv_alive:
            return
        try:
            self._player.show_text(text, str(duration), level)
        except _mpv_errors:
            self._handle_mpv_disconnect()

    _default_osd_back_color = "#C8000000"
    _default_osd_font_size = 55

    def get_osd_settings(self):
        if not self._mpv_alive:
            return self._default_osd_back_color, self._default_osd_font_size, None
        try:
            # osd-border-style was added in mpv ~0.34. Tolerate it being absent.
            try:
                border_style = self._player.osd_border_style
            except Exception:
                border_style = None
            return (
                self._player.osd_back_color or self._default_osd_back_color,
                self._player.osd_font_size or self._default_osd_font_size,
                border_style,
            )
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return self._default_osd_back_color, self._default_osd_font_size, None

    def set_osd_settings(self, back_color: str, font_size: int, border_style=None):
        if not self._mpv_alive:
            return
        try:
            self._player.osd_back_color = back_color
            self._player.osd_font_size = font_size
            # Required to make osd-back-color actually render as a filled box
            # on mpv 0.36+ where the default shifted to outline-and-shadow.
            # If the caller doesn't have a saved value (e.g. the original read
            # failed at OSDMenu init), fall back to the modern mpv default
            # rather than leaving the property at whatever the menu set it to.
            try:
                self._player.osd_border_style = border_style or "outline-and-shadow"
            except Exception:
                pass  # Older mpv that lacks the property; nothing to restore.
        except _mpv_errors:
            self._handle_mpv_disconnect()

    def enable_osc(self, enabled: bool):
        if settings.mpv_ext and settings.mpv_ext_no_ovr:
            return  # Don't override user's MPV config

        if not self._mpv_alive:
            return
        try:
            if self._osc_script_loaded:
                # Both shim OSC scripts register the osc-visibility message.
                self.script_message(
                    "osc-visibility", "auto" if enabled else "never", "False"
                )
                if hasattr(self._player, "osc"):
                    self._player.osc = False
            else:
                if hasattr(self._player, "osc"):
                    # The mpvtk playback HUD replaces any OSC — never
                    # turn the built-in one on under it.
                    self._player.osc = (
                        enabled and settings.osc_style != "mpvtk"
                    )
        except _mpv_errors:
            self._handle_mpv_disconnect()

    def triggered_menu(self, enabled: bool):
        self.script_message("shim-menu-enable", "True" if enabled else "False")

    def playback_is_aborted(self):
        try:
            return self._player.playback_abort
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return True

    def set_browse_window(self, enabled: bool):
        """Persistent window for the in-window mpvtk browser.

        With the in-window UI there is exactly one window, and its state is
        the product of two mpv properties:

            state                          playback_abort  force_window
            library browser                yes             yes
            media playing                  no              yes
            "minimized" (tray only)        yes             no
            cast to, library not open      no              no

        ``set_browse_window(True)`` is the first row; ``False`` drops to the
        third (or leaves the fourth alone, since it won't touch the window
        while something is playing). Minimizing is therefore not a
        window-manager action — it's releasing force_window with nothing to
        play, which is also why the app stays a usable cast target while
        minimized.

        Differs from force_window() (used by the OSD menu) in two ways the
        browser needs: no Jellyfin logo splash (the browser paints its own
        background) and free resizing (keepaspect-window=no, like the mpvtk
        demo) so the window doesn't snap to a media aspect ratio."""
        if not self._mpv_alive:
            if not enabled:
                return
            self._init_mpv()
        try:
            if enabled:
                try:
                    # The background is a solid colour, so stretch it to fill
                    # the window. Without this mpv letterboxes the (square)
                    # image and the bars around it show as a grey frame under
                    # the UI. Restored by browse_yield() for real video.
                    self._player.keepaspect = False
                except Exception:
                    pass  # older mpv without the property; harmless
                self._player.force_window = True
                self._player.keep_open = True
                self._player.image_display_duration = "inf"
                # Reloading the background while it is already up tears the
                # video output down and back up, which reads as the window
                # closing and reopening. Stopping playback hits this path
                # twice (once from the stopped-playstate callback, once from
                # the caller), so it has to be idempotent.
                if not (self._showing_browse_bg and self._video is None):
                    self._player.play(_browse_background())
                    self._showing_browse_bg = True
                # Browsing is a desktop-UI activity: only go fullscreen if the
                # user explicitly asked for a fullscreen browser. settings.
                # fullscreen still applies when playback starts.
                if settings.browser_fullscreen:
                    self._player.fs = True
                elif not self._video:
                    self._player.fs = False
            else:
                try:
                    self._player.keepaspect = True
                except Exception:
                    pass
                self._showing_browse_bg = False
                self._player.image_display_duration = 1
                self._player.keep_open = False
                if not self._video:
                    self._set_force_window(False)
                    self._player.command("stop")
        except _mpv_errors:
            self._handle_mpv_disconnect()

    def raise_window(self):
        """Best-effort "bring the player window forward" — the tray's Show
        action and a second app launch both need it. Windows has a real API
        for this via pywin32; elsewhere the most we can portably do is
        un-minimize, since raising is the window manager's call."""
        if not self._mpv_alive:
            return
        if win_utils is not None:
            try:
                win_utils.raise_mpv()
                return
            except Exception:
                log.debug("win_utils.raise_mpv failed", exc_info=True)
        try:
            self._player.window_minimized = False
        except Exception:
            log.debug("could not un-minimize the player window", exc_info=True)

    def browse_yield(self):
        """Hand the window from the in-window browser back to playback.

        Undoes only the parts of set_browse_window() that would harm video —
        the stretched aspect and the non-fullscreen browse window. It must not
        touch force_window/keep_open: playback is still starting up here and
        tearing the window down would kill it."""
        if not self._mpv_alive:
            return
        self._showing_browse_bg = False
        try:
            self._player.keepaspect = True
            if settings.fullscreen and not self.fullscreen_disable:
                self._player.fs = True
        except _mpv_errors:
            self._handle_mpv_disconnect()
        except Exception:
            log.debug("browse_yield failed", exc_info=True)

    def force_window(self, enabled: bool):
        if not self._mpv_alive:
            if not enabled:
                return
            log.info("mpv is dead, reinitializing for menu window")
            self._init_mpv()
        try:
            if enabled:
                self._player.force_window = True
                self._player.keep_open = True
                self._player.image_display_duration = "inf"
                self._player.play(get_resource("logo.png"))
                if settings.fullscreen:
                    self._player.fs = True
            else:
                self._player.image_display_duration = 1
                self._player.keep_open = False
                if not self._video:
                    self._set_force_window(False)
                    self._player.command("stop")
                elif self._player.playback_abort:
                    self._set_force_window(False)
                    self._player.play("")
                else:
                    self.upd_player_hide()
        except _mpv_errors:
            self._handle_mpv_disconnect()

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

    # Jellyfin remote navigation (MoveUp/Select/… from a phone or web
    # client) -> mpv key names. While the mpvtk browser owns input its
    # forced nav bindings catch these; during video playback they fall
    # through to kb_seek as before.
    _NAV_KEYPRESS = {"up": "UP", "down": "DOWN", "left": "LEFT",
                     "right": "RIGHT", "ok": "ENTER", "back": "ESC"}

    # Remote commands the in-window browser answers with a real page. The
    # OSD menu has neither, so for it both still just open the menu.
    _NAV_COMMANDS = ("home", "settings")
    _MENU_ALIAS = {"settings": "home"}

    def _nav_command(self, action):
        handler = self.on_nav_command
        if handler is None or not self.mpvtk_active or self._video is not None:
            return False
        try:
            return bool(handler(action))
        except Exception:
            log.debug("nav command %r failed", action, exc_info=True)
            return False

    def _nav_back(self):
        handler = self.on_nav_back
        if handler is None or not self.mpvtk_active or self._video is not None:
            return False
        try:
            return bool(handler())
        except Exception:
            log.debug("nav back handler failed", exc_info=True)
            return False

    def _mpvtk_userdata(self, prop):
        if not self.mpvtk_active or self._player is None:
            return False
        try:
            if is_using_ext_mpv:
                return bool(self._player.command("get_property", prop))
            return bool(self._player._get_property(prop))
        except Exception:
            return False

    def _mpvtk_input_active(self):
        """True while the in-window UI's key bindings are live (the
        renderer mirrors it into user-data on every transition)."""
        return self._mpvtk_userdata("user-data/mpvtk/active")

    def _mpvtk_hud_idle(self):
        """True while the playback HUD is attached but hidden: remote
        Move*/Select should reach the renderer's summon bindings (the
        first press shows the HUD) instead of acting as seek keys.
        Back keeps its stop-to-browser meaning while hidden."""
        return self._mpvtk_userdata("user-data/mpvtk/hud")

    def menu_action(self, action):
        if self.menu.is_menu_shown:
            self.menu.menu_action(self._MENU_ALIAS.get(action, action))
        elif action in self._NAV_COMMANDS and self._nav_command(action):
            pass    # the in-window UI has its own home / settings pages
        elif action in self._NAV_KEYPRESS and (
            self._mpvtk_input_active()
            or (action != "back" and self._mpvtk_hud_idle())
        ):
            # remote drives the browser's spatial navigation (or, while
            # the playback HUD is hidden, summons it)
            try:
                self._player.command(
                    "keypress", self._NAV_KEYPRESS[action])
            except Exception:
                log.debug("nav keypress failed", exc_info=True)
        else:
            # No in-window UI (CLI / Tk / mid-playback): "settings" keeps its
            # historical meaning of opening the OSD menu, which is the only
            # settings surface those paths have.
            self.kb_seek(self._MENU_ALIAS.get(action, action))


playerManager = PlayerManager()
