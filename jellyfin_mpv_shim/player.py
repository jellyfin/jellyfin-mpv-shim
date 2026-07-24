import logging
import os
import re
import sys
import time
import json

import platform

from threading import RLock, Lock, Thread, Event
from queue import Queue, Empty as queue_empty
from collections import OrderedDict, deque
from typing import TYPE_CHECKING, Optional

from . import conffile
from .utils import synchronous, Timer, none_fallback, get_resource
from .mpv_events import wait_property
from .session_reporter import SessionReporter
from .conf import settings
from .menu import OSDMenu
from .osc_bridge import OscBridge
from .constants import APP_NAME, DESKTOP_ID, USER_APP_NAME
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

# How long to wait for an mpv command's reply once the window is going
# away. Only the external (jsonipc) backend needs this: every command
# there is a request/response over a socket, and the reply is waited for
# with python_mpv_jsonipc.TIMEOUT, which is 120s.
#
# A closing window puts that squarely in the failure path. mpv can accept
# a command, run it, and exit before its reply is written back — we saw
# exactly that on the close path, where trickplay's overlay-clear reached
# mpv (it logged "Clearing trickplay") but the reply never came, parking
# the action thread for two minutes with the whole shutdown queued behind
# it. libmpv has no equivalent: a dead handle raises immediately, which
# is why the same close is instant there.
#
# Bounding the wait is the fix rather than hunting individual calls: any
# command issued while the window is disappearing can lose its reply, and
# during teardown there is no command whose answer is worth minutes.
IPC_TEARDOWN_TIMEOUT = 5

# --- Audio output modes ---------------------------------------------------
#
# "auto" is the default and is defined by doing *nothing*: no audio-channels,
# no audio-spdif, no filters. mpv's own defaults (and anything in the user's
# mpv.conf) are left entirely alone. The other modes each describe a physical
# connection to a receiver.
#
# Which codecs a mode can pass through is a property of the cable. S/PDIF
# (optical/coax) carries ~1.5 Mbps, which fits AC3 and DTS core and nothing
# else; HDMI has the bandwidth for the high-bitrate and lossless formats too.
AUDIO_PASSTHROUGH_CODECS = {
    "optical": ("ac3", "dts"),
    "hdmi": ("ac3", "dts", "eac3", "dts-hd", "truehd"),
}

# What each mode sets audio-channels to. "auto" is absent on purpose.
AUDIO_MODE_CHANNELS = {
    "stereo": "2.0",
    "optical": "5.1,2.0",
    "hdmi": "7.1,5.1,2.0",
}

# Filter labels. Labelled so they can be removed again -- jellyfin-media-player
# added its AC3 encoder unlabelled but removed "@ac3", which never matched, so
# once switched on the filter stayed for the rest of the session.
AF_NIGHT_MODE = "jfnight"
AF_AC3_ENCODE = "jfac3"

# Night mode. dynaudnorm rather than loudnorm or acompressor: it is the one
# designed for real-time use (loudnorm's single-pass mode buffers and drifts),
# and it lifts quiet dialogue as well as taming loud effects. These are the
# widely-used mpv night-mode values.
NIGHT_MODE_FILTER = "dynaudnorm=g=5:f=250:r=0.9:p=0.5"

# Encoding to AC3 is the only way surround crosses an optical cable when the
# track is not already AC3 or DTS. minch=3 makes the filter detach itself for
# stereo content, which should just go out as PCM.
AC3_ENCODE_FILTER = "lavcac3enc=minch=3"


def audio_passthrough_enabled(codec: str) -> bool:
    """Whether the user has left ``codec`` (mpv's spelling) ticked."""
    return bool(getattr(settings, "audio_passthrough_" + codec.replace("-", "_"), False))


def audio_spdif_codecs(mode: str, night_mode: bool, enabled=audio_passthrough_enabled):
    """The codec list for mpv's ``audio-spdif``, for ``mode``.

    Empty whenever night mode is on. Passthrough hands the receiver an
    undecoded compressed stream, and a PCM filter cannot run downstream of
    one -- mpv does not arbitrate between the two, the chain fails to build
    ("unsupported conversion: spdif-ac3 -> floatp") and mpv recovers by
    disabling the filter. So asking for both does not break playback; it
    makes night mode silently do nothing, which is worse than it sounds
    because there is no user-visible sign of it.
    """
    if night_mode:
        return []
    codecs = [c for c in AUDIO_PASSTHROUGH_CODECS.get(mode, ()) if enabled(c)]
    # Per mpv's manual, specifying both dts and dts-hd "behaves equivalent to
    # specifying dts-hd only". Drop the redundant entry so the value we set
    # reads the way it actually behaves.
    if "dts-hd" in codecs:
        codecs = [c for c in codecs if c != "dts"]
    return codecs


def audio_wants_ac3_encode(
    mode: str,
    track_codec: Optional[str],
    spdif_codecs,
    encode_others: bool = True,
    ac3_ok: bool = True,
) -> bool:
    """Whether ``lavcac3enc`` belongs in the chain for the current track.

    Only optical: HDMI carries multichannel PCM natively, so re-encoding
    there would throw away quality for nothing.

    The decision is per-track and cannot be made once at startup, which is
    the trap jellyfin-media-player fell into. Handing mpv both audio-spdif
    and lavcac3enc for the same track builds a chain it cannot satisfy
    ("unsupported conversion: spdif-ac3 -> floatp"); mpv recovers by
    disabling the filter, so the cost is that the filter silently does not
    apply -- the encoder, or night mode, quietly stops working. Pass the
    track through if we can, and reach for the encoder only for the ones we
    can't.

    ``encode_others`` off declines the encoder entirely; those tracks go out
    as stereo PCM, since S/PDIF cannot carry multichannel PCM either. That
    loses surround, but the encoder adds latency on some receivers.

    ``ac3_ok`` is the AC3 passthrough toggle, and it gates this too: the
    encoder emits an IEC61937 AC3 *bitstream*, not PCM, so a user who
    unticked AC3 because their receiver cannot decode it must not be sent
    AC3 by the back door.
    """
    if mode != "optical" or not encode_others or not ac3_ok:
        return False
    if track_codec and track_codec.lower() in spdif_codecs:
        return False
    return True


def bound_ipc_replies(seconds=IPC_TEARDOWN_TIMEOUT):
    """Stop waiting minutes for replies from an mpv that is going away.

    ``TIMEOUT`` is a module global read at each wait, so lowering it takes
    effect for calls already in flight as well as later ones. Idempotent,
    and never raised back to the caller: this runs on teardown paths where
    failing to tighten a timeout must not become the thing that breaks the
    shutdown.
    """
    if not is_using_ext_mpv:
        return
    try:
        if mpv.TIMEOUT > seconds:
            log.debug("Bounding mpv IPC reply wait to %ss for teardown.",
                      seconds)
            mpv.TIMEOUT = seconds
    except Exception:
        log.debug("Could not bound the mpv IPC reply wait.", exc_info=True)

# The mpvtk browser's window background. mpv paints it directly
# (background=color), so nothing has to be decoded to hold the window open.
BROWSE_BG_HEX = "#141414"
# mpv's own defaults, restored by browse_yield() when video takes the window
# back. Kept here so the browse background can't leak into playback.
MPV_DEFAULT_BACKGROUND = "tiles"
MPV_DEFAULT_BACKGROUND_HEX = "#000000"


def runtime_force_window_works(version):
    """Whether this mpv acts on a force-window change made while idle.

    Every mpv *stores* the property; only 0.41 and newer create or destroy
    the video output for it. Older builds decide at startup and then never
    revisit it, which is why the window has to be asked for on the command
    line (see ``_init_mpv``) and why releasing it later does nothing.

    Historically none of this mattered: the window was summoned by loading
    a file and released by unloading one, so force_window was only ever a
    flag alongside real media. ``PlayerManager.force_window`` still works
    that way. The browser stopped loading anything -- deliberately, since
    reloading a background file tears the video output down and reads as
    the window closing and reopening -- and inherited the newer behaviour
    without anyone noticing the version it needs.

    An unreadable version is treated as old, because the two ways of being
    wrong are not symmetric: assuming old costs a fallback that works
    everywhere, assuming new costs a window that will not go away.
    """
    m = re.search(r"(\d+)\.(\d+)", version or "")
    if not m:
        return False
    return (int(m.group(1)), int(m.group(2))) >= (0, 41)


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


# Recent error-level lines from mpv, so a failed load can tell the user *why*
# it failed. The end-file event carries only a coarse reason ("error"); the
# actual cause ("tls: Error decoding the received TLS packet", "Failed to open
# ...") arrives solely through mpv's log. deque append/clear are atomic, which
# matters because mpv's event thread writes this while a pool worker reads it.
_recent_mpv_errors = deque(maxlen=8)


def clear_mpv_errors():
    """Drop stale errors so a failed load can't report the previous file's."""
    _recent_mpv_errors.clear()


def last_mpv_error():
    """The most recent error line mpv logged, or None."""
    try:
        return _recent_mpv_errors[-1]
    except IndexError:
        return None


def mpv_log_handler(level: str, prefix: str, text: str):
    message = "{0}: {1}".format(prefix, text)
    if level in ("fatal", "error"):
        _recent_mpv_errors.append(message.strip())
    if level in mpv_log_levels:
        mpv_log_levels[level](message)
    else:
        mpv_log.debug(message)


# MPV_END_FILE_REASON_*, for backends that deliver the reason as a raw int.
_END_FILE_REASONS = {0: "eof", 2: "stop", 3: "quit", 4: "error", 5: "redirect"}


def _decode_reason(value):
    """Coerce an end-file reason to a lowercase string, or None if unreadable.

    The value's shape depends on the backend and the python-mpv release: a
    str, bytes, an int, or a Reason enum. Returning None (rather than
    guessing) matters — the caller only acts on a confident "error", so an
    unrecognized shape must degrade to the timeout path, never to aborting a
    load that is actually fine.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace").lower()
        except Exception:
            return None
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return _END_FILE_REASONS.get(value)
    # Enum (python-mpv's MpvEventEndFile.Reason): prefer the name, fall back
    # to the numeric value.
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name.lower()
    return _decode_reason(getattr(value, "value", None))


def end_file_info(event):
    """(reason, detail) for an mpv end-file event, normalized across backends.

    libmpv delivers an event object whose layout has shifted across
    python-mpv releases; external mpv delivers a plain dict. Never raises:
    this runs on mpv's event thread, where an exception would take out every
    other observer with it.
    """
    reason = detail = None
    try:
        data = event.as_dict() if hasattr(event, "as_dict") else event
        if isinstance(data, dict):
            # Some shapes nest the payload under "event", others are flat.
            inner = data.get("event")
            if not isinstance(inner, dict):
                inner = data
            reason = _decode_reason(inner.get("reason", data.get("reason")))
            detail = (inner.get("file_error") or inner.get("error")
                      or data.get("file_error") or data.get("error"))
        else:
            payload = getattr(event, "data", None)
            reason = _decode_reason(getattr(payload, "reason", None))
            detail = getattr(payload, "error", None)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        elif not isinstance(detail, str):
            # The struct path reports `error` as a raw libmpv error code, and
            # showing the user "-13" is worse than showing nothing — the line
            # mpv logged (last_mpv_error) is the readable source, and the
            # caller falls back to it when this is None.
            detail = None
    except Exception:
        log.debug("Could not decode end-file event.", exc_info=True)
        return None, None
    return reason, detail


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
def _server_uuid_of(video):
    """The uuid of the server a playing item came from, or None."""
    try:
        from .clients import clientManager
        client = getattr(video, "client", None)
        if client is None:
            return None
        for uuid, candidate in clientManager.clients.items():
            if candidate is client:
                return uuid
    except Exception:
        log.debug("could not resolve the playing item's server",
                  exc_info=True)
    return None


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
        # Whether we have pushed any audio setting to the current mpv
        # instance. Gates the "Default (auto) touches nothing" fast path in
        # apply_audio_settings.
        self._audio_configured = False
        # mpv's audio config as it was before we first touched it, so
        # returning to Default can put it back. See _snapshot_audio_state.
        self._audio_snapshot = None
        # Serializes the audio settings read + the mpv writes it implies.
        self._audio_lock = RLock()
        self._lock = RLock()
        self._tl_lock = RLock()
        self._finished_lock = Lock()
        self.last_update = Timer()
        self._jf_settings = None
        self.pause_ignore = None  # Used to ignore pause events that come from us.
        self.do_not_handle_pause = False
        # Throttle for periodic offline resume-position persistence on the
        # timeline path (time.monotonic seconds); -inf so the first tick fires.
        self._last_offline_record = float("-inf")
        self.last_seek = None
        self.warned_about_transcode = False
        self.fullscreen_disable = False
        # The geometry option value mpv currently holds. Tracked rather than
        # read back, because writing it is a resize command (see
        # _sync_window_geometry) and a redundant write is not free.
        self._geometry_armed = None
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
        # Skippable segment the playback HUD should offer a button for
        # (an Intro object, or None).
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
        # Load-failure detection. mpv signals an unloadable file with an
        # end-file event, but the duration wait that gates playback startup
        # would otherwise sit out its full timeout waiting for a value that
        # can never arrive — the load generation lets the handler ignore an
        # end-file belonging to the file we just replaced.
        # Session reports (playing/stopped) go out here rather than inline:
        # they are remote round trips that used to sit on the advance path.
        # Never drain() while holding _tl_lock — _session_playing_safe takes
        # it on the worker.
        self._reporter = SessionReporter()
        self._load_failed = Event()
        # The other way out of that wait. duration is a proxy for "the file is
        # loaded" dating back to the initial commit, and it is a bad one for
        # anything unbounded: a live stream never reports one, so the wait sat
        # out its full timeout and killed a stream that was playing fine.
        # file-loaded says the same thing directly, and mpv has the track list
        # populated by the time it fires, which is what the code after the
        # wait actually needs.
        self._load_completed = Event()
        self._load_error_detail = None
        self._load_generation = 0
        self._loading = False
        self._load_cancelled = False
        # A browse-window setup that had to skip its `stop` because a start
        # was in flight; applied by _abort_load if that start never plays.
        self._browse_bg_deferred = False
        # True for the whole of a start, including the PlaybackInfo round trip
        # that precedes _loading. This is what Cancel is gated on.
        self._start_in_progress = False
        # Set when a video starts; the action thread fires the trickplay tile
        # fetch off it once playback is live (see _pump_trickplay).
        self._trickplay_pending = False
        # (video, offset) of the last failed start, so the UI's error dialog
        # can retry it. Cleared once a retry is dispatched or a play succeeds.
        self._failed_playback = None
        # Called with a dict when a load starts / fails, so the UI can show a
        # loading screen and then an error with retry options. Set by the
        # browser; None in CLI mode, where the log is the only surface.
        self.on_load_start = None
        self.on_load_error = None
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
        # Stall watchdog state: (position, when it was first seen).
        # Feeds _check_stalled_finish, which covers an end-of-file that mpv
        # never reports at all — distinct from the poll rescue below it, which
        # covers one that was reported but whose notification was lost.
        self._stall_position = None
        self._stall_since = 0.0
        # Timestamp of the most recent intro/credits prompt or skip toast.
        # Used to debounce the prompt loop so a skip event isn't immediately
        # overwritten by a "Seek to Skip Credits" prompt when the post-skip
        # position lands inside an outro segment (common on short videos).
        self._last_intro_msg_time = 0.0

        # Optional callback (set by the UI) fed a compact now-playing dict on
        # every playback state change, for the browser's music bar. Kept as a
        # plain attribute so the player has no hard dependency on the GUI.
        self.on_playstate = None
        # Set True while the in-window mpvtk browser owns the window (browse
        # mode, and the cast screen). Guards idle_quit so an on-screen UI
        # never has the window torn out from under it.
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
        # renderer, so it gets explicit hooks.
        #
        # TWO phases, and the distinction is load-bearing:
        #
        #   on_mpv_gone       - the handle is no longer OURS. Stop pushing to
        #                       it. mpv itself may still be running: terminate
        #                       happens on its own thread, so this fires while
        #                       the process is on its way out.
        #   on_mpv_terminated - mpv is actually dead. Only now is it safe to
        #                       free anything mpv reads BY ADDRESS, i.e. the
        #                       in-process BGRA tile buffers. Freeing them at
        #                       on_mpv_gone time released memory a live mpv
        #                       was still compositing from every frame, which
        #                       is a segfault on quit.
        #
        # on_mpv_recreated fires once a fresh handle is ready.
        self.on_mpv_gone = None
        self.on_mpv_terminated = None
        self.on_mpv_recreated = None
        # BACK/ESC handler for the in-window UI. Returns True when it
        # consumed the press; at the root of its nav stack it declines and
        # ESC keeps its old meaning (leave fullscreen).
        self.on_nav_back = None
        # Remote menu commands the in-window UI answers itself ("home",
        # "settings"). Returns True when handled.
        self.on_nav_command = None
        # Opens the playback HUD's gear menu (set by mpvtk_browser.ui).
        # During video under the in-window OSC, the kb_menu key routes
        # here instead of the OSD menu. Returns True when handled.
        self.on_hud_menu = None
        # Optional callback (set by the UI) invoked (version, url) when an
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

        # Which in-player UI to load: the in-window mpvtk playback HUD
        # ("mpvtk"; no lua script — the browser renders it, see
        # mpvtk_browser/hud.py), the stock mpv OSC patched with
        # trickplay previews ("mpv"), or none ("default": whatever the
        # mpv binary ships / the user's own scripts). "jellyfin" is a
        # legacy alias for the HUD — the jellyfin-styled lua OSC it
        # used to name was retired once the HUD reached parity.
        osc_style = settings.osc_style
        if osc_style == "jellyfin":
            osc_style = "mpvtk"
        if osc_style == "mpvtk" and not settings.enable_gui:
            # The playback HUD is rendered by the library browser; with the
            # GUI disabled there is nothing to render it, so the patched
            # stock OSC is the closest thing.
            osc_style = "mpv"
        if osc_style == "mpvtk" and not settings.thumbnail_osc_builtin:
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
        # Resolved style for this mpv instance (settings may hold the
        # legacy alias / a fallback may have applied) — the c-menu
        # routing, enable_osc and the skip-button path key off it.
        self._osc_style_resolved = osc_style
        if osc_style == "mpv":
            scripts.append(get_resource("trickplay-osc.lua"))
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

        # Window title. mpv's default is "No file - mpv", which names the
        # wrong application and reports "No file" for what is actually the
        # library browser. Property expansion is mpv's, evaluated live, so
        # the title follows playback without us pushing updates.
        mpv_options["title"] = "${?media-title:${media-title} - }%s" % USER_APP_NAME

        # Window size. mpv defaults to a fixed 960x540 whatever the display
        # size, which is cramped for a browsable UI. Restored from the last
        # session when remember_window_size is on (see _save_window_geometry).
        width = max(320, int(settings.window_width or 1280))
        height = max(240, int(settings.window_height or 720))
        mpv_options["geometry"] = "%dx%d" % (width, height)
        self._geometry_armed = mpv_options["geometry"]
        if settings.window_maximized:
            mpv_options["window_maximized"] = True
        # geometry is documented as an INITIAL size, but X11 re-applies it on
        # every VO reconfig (rc = geo.win whenever geometry.wh_valid), so a
        # window the user resized snapped back to the stored size on the next
        # file. _sync_window_geometry keeps the armed value equal to the live
        # size so that re-apply is a no-op; it must never be *cleared* at
        # runtime — see the comment there. auto-window-resize is the other
        # half: without it mpv fills the gap by resizing to each video's
        # native size, which is what geometry had been masking. Both are
        # needed — per mpv's own docs, auto-window-resize "does not have any
        # impact on the --geometry option".
        mpv_options["auto_window_resize"] = False

        # The in-window UI has to ask for its window on the command line.
        #
        # mpv before 0.41 accepts a runtime force-window change and stores it,
        # but never acts on it while idle: the VO is created only if the
        # option was set at startup, and once created it can no longer be
        # released. Measured on 0.40.0 vs 0.41.0 -- with --idle and no file,
        # setting force-window over IPC leaves `vo-configured` false on 0.40
        # and flips it true on 0.41. It is a version difference, not a backend
        # one; the libmpv path only looked fine here because the installed
        # libmpv was newer than the mpv binary. So on 0.40 set_browse_window
        # raised no window at all, and with the browser being the window's
        # entire content the app came up invisible and the tray's Show
        # Library Browser had nothing to show.
        #
        # First launch takes the window unless start_minimized asked for the
        # windowless state. A re-open (crash recovery, idle-quit) takes it
        # only if the browser was on screen: the play path doesn't need this,
        # because loading a file brings the VO up on its own.
        #
        # Only force_window is passed here, not the browse background --
        # background=color needs mpv 0.38, and an unknown option makes mpv
        # exit at startup rather than raise something recoverable.
        # set_browse_window applies the background a moment later.
        if osc_style == "mpvtk":
            if self.mpvtk_active if reopen else not settings.start_minimized:
                mpv_options["force_window"] = True

        # Desktop-icon hints. mpv has no "set the window icon" option; on
        # Linux the icon is resolved by matching the window's class against
        # an installed .desktop file, so naming ourselves after ours is the
        # whole mechanism. Only meaningful once the .desktop is installed
        # (packaged/Flatpak, not run-from-source), and some window managers
        # still prefer mpv's built-in _NET_WM_ICON — overriding that needs
        # Xlib, which is not worth a dependency.
        #
        # Platform-gated: --x11-name only exists in builds with X11 support,
        # so setting it on a Windows or macOS mpv fails at startup. Those
        # platforms take their icon from the exe/bundle anyway.
        if sys.platform not in ("win32", "darwin"):
            mpv_options["x11_name"] = DESKTOP_ID
            mpv_options["wayland_app_id"] = DESKTOP_ID

        self._player = mpv.MPV(
            input_default_bindings=True,
            input_vo_keyboard=True,
            input_media_keys=settings.media_keys,
            log_handler=mpv_log_handler,
            loglevel=settings.mpv_log_level,
            **mpv_options,
        )

        try:
            self._runtime_force_window = runtime_force_window_works(
                self._player.mpv_version)
        except Exception:
            log.debug("could not read the mpv version", exc_info=True)
            self._runtime_force_window = False
        if not self._runtime_force_window:
            log.info("This mpv cannot give up its window on request "
                     "(needs 0.41+); minimizing will quit mpv instead.")

        # The menu object must survive mpv re-creation (crash recovery,
        # idle-quit): its is_menu_shown state gates idle_quit, and callers
        # outside this class hold on to it. A fresh OSDMenu here used to reset
        # is_menu_shown to False mid-show, letting idle_quit kill the window
        # while the user was looking at the menu.
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

        # Fresh mpv instance: nothing has been applied to it yet, and the
        # previous instance's snapshot describes a player that no longer
        # exists (re-open, crash recovery).
        self._audio_configured = False
        self._audio_snapshot = None
        try:
            self.apply_audio_settings()
        except Exception:
            # Never abort _init_mpv over audio config. We are past mpv's
            # construction but before _mpv_alive and the event/key bindings,
            # so escaping here would leave a live mpv window the shim does
            # not drive and _ensure_mpv would later start a second one on top
            # of it. Every other optional property write here is guarded the
            # same way.
            log.error("Could not apply audio settings at startup.", exc_info=True)

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
            # From here mpv may exit at any moment, including between
            # accepting a command and answering it. Everything below runs
            # on the action thread, so an unbounded reply wait blocks the
            # whole shutdown behind it — see bound_ipc_replies.
            bound_ipc_replies()
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
            if getattr(self, "_osc_style_resolved", None) == "mpvtk":
                # Under the in-window OSC the HUD's gear menu replaces the
                # OSD menu entirely. The OSD menu is a classic-OSC surface:
                # drawn as mpv OSD text, it lands *under* the mpvtk overlay
                # bitmaps and steals the arrow keys from the browser, so it
                # must not open here even when the HUD declines (browsing,
                # idle, no video).
                if self._video is not None and self.on_hud_menu is not None:
                    try:
                        self.on_hud_menu()
                    except Exception:
                        log.debug("hud menu open failed", exc_info=True)
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

        @self._player.event_callback("file-loaded")
        def handle_file_loaded(_event):
            # Mirrors handle_end_file's generation guard: a file-loaded from
            # the OUTGOING file (keep_open holds it until the replacement
            # lands) must not be taken as the incoming one having loaded.
            if self._loading:
                self._load_completed.set()
            # Whether the AC3 encoder belongs in the chain depends on this
            # file's audio codec. Deferred onto the action thread: issuing mpv
            # commands from inside an event handler is what put_task exists to
            # avoid.
            self.put_task(self.apply_audio_filters)

        @self._player.property_observer("current-tracks/audio/codec")
        def handle_audio_codec_change(_name, _value):
            # Track switches change the answer as much as file changes do:
            # moving from an AC3 track (passed through) to a 5.1 AAC one needs
            # the encoder attached, or the surround is silently lost. Observed
            # rather than hooked into set_streams so that mpv's own track
            # cycling is covered too.
            self.put_task(self.apply_audio_filters)

        @self._player.event_callback("end-file")
        def handle_end_file(event):
            # Only interesting while a load is in flight: this is purely a
            # shortcut out of the duration wait. Normal end-of-playback stays
            # with the eof-reached / playback-abort observers, which own the
            # queue-advance logic.
            generation = self._load_generation
            if not self._loading:
                return
            reason, detail = end_file_info(event)
            # Strictly "error". A file being replaced mid-playback ends with
            # "stop"/"redirect", and treating either as a failure would abort
            # a perfectly good load; anything unrecognized decodes to None and
            # falls through to the timeout, which is the safe direction.
            if reason != "error":
                return
            # Re-check the generation after the (slow-ish) decode: a stale
            # end-file from the outgoing file must not fail the incoming one.
            if generation != self._load_generation or not self._loading:
                return
            log.error("mpv reported a load error: %s", detail or "no detail")
            self._load_error_detail = detail
            self._load_failed.set()

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
            except Exception:
                log.warning("Error when processing client-message.", exc_info=True)

        self._showing_browse_bg = False
        if settings.enable_gui:
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

    # -- Audio output configuration ---------------------------------------

    def _mpv_property(self, prop):
        """Read an mpv property by its full (path) name on either backend."""
        if self._player is None:
            return None
        try:
            if is_using_ext_mpv:
                return self._player.command("get_property", prop)
            return self._player._get_property(prop)
        except Exception:
            return None

    def _attached_af_labels(self):
        """Labels currently in mpv's audio filter chain.

        None if the chain could not be read, which callers treat as "don't
        know" rather than "empty".
        """
        chain = self._mpv_property("af")
        if not isinstance(chain, (list, tuple)):
            return None
        return {
            entry.get("label")
            for entry in chain
            if isinstance(entry, dict) and entry.get("label")
        }

    def _set_af(self, label: str, filter_spec: Optional[str]):
        """Add or remove a labelled audio filter, idempotently.

        ``af remove`` on a label that isn't attached still succeeds, but mpv
        logs "Option af-remove: item label @x not found" at warn level for it
        -- which the shim surfaces, so an unconditional remove meant two
        warnings on every night-mode toggle and one per file in optical mode.
        Ask what is attached first, and skip the removal when there is
        nothing to remove. Reading the chain rather than tracking it in
        Python is deliberate: mpv drops a filter that fails to initialize, so
        our idea of what is attached can otherwise drift from the truth.
        """
        if self._player is None:
            return
        try:
            attached = self._attached_af_labels()
            # None => unreadable; fall back to the unconditional remove, which
            # is correct, just noisy.
            if attached is None or label in attached:
                self._player.command("af", "remove", "@" + label)
            if filter_spec:
                self._player.command("af", "add", "@%s:%s" % (label, filter_spec))
        except _mpv_errors:
            raise
        except Exception:
            log.error("Could not update audio filter %s.", label, exc_info=True)

    # The properties apply_audio_settings writes, and therefore the ones it
    # has to be able to put back. Keyed by mpv name, valued by the attribute
    # name the backends expose (both accept underscores).
    _AUDIO_PROPS = {
        "audio-channels": "audio_channels",
        "audio-normalize-downmix": "audio_normalize_downmix",
        "audio-spdif": "audio_spdif",
    }

    def _snapshot_audio_state(self):
        """Record mpv's audio config before we first overwrite it.

        Whatever is in place at this point came from the user's own mpv.conf
        (or mpv's defaults), and returning to "Default (auto)" has to give it
        back. Restoring hardcoded defaults instead would silently discard an
        `audio-spdif=ac3,dts` the user had configured themselves -- and there
        would be no way to get it back short of restarting.
        """
        if self._audio_snapshot is not None:
            return
        self._audio_snapshot = {
            prop: self._mpv_property(prop) for prop in self._AUDIO_PROPS
        }

    def _restore_audio_state(self):
        snapshot = self._audio_snapshot or {}
        for prop, attr in self._AUDIO_PROPS.items():
            value = snapshot.get(prop)
            if value is not None:
                setattr(self._player, attr, value)

    def apply_audio_settings(self):
        """Push the audio output mode to mpv.

        Called once per mpv instance and again whenever the settings change.
        In "auto" mode this sets nothing: the point of that mode is that a
        user who configured audio in their own mpv.conf is left alone.

        The per-track half of the job (whether to engage the AC3 encoder)
        happens in apply_audio_filters, because it depends on what is
        playing.
        """
        if self._player is None:
            return
        mode = settings.audio_mode or "auto"
        night = bool(settings.audio_night_mode)
        # One lock around the settings read and the writes it implies. Without
        # it a file loading on the action thread and a toggle on the browser
        # thread can interleave into a config neither of them asked for, and
        # nothing re-runs to correct it. Not _lock, which is held across a
        # whole playback start.
        with self._audio_lock:
            try:
                if mode == "auto" and not night and not self._audio_configured:
                    # Nothing applied to this mpv instance and nothing asked
                    # for: leave it completely alone. Once we *have* touched
                    # it the branch below runs instead, so returning to
                    # Default undoes our changes rather than stranding them.
                    return
                self._snapshot_audio_state()
                self._audio_configured = True
                channels = AUDIO_MODE_CHANNELS.get(mode)
                codecs = audio_spdif_codecs(mode, night)
                if mode == "auto":
                    # Hand back whatever the user had, then re-apply only what
                    # night mode genuinely requires (it cannot run on a
                    # passthrough stream, so passthrough has to go).
                    self._restore_audio_state()
                    if night:
                        self._player.audio_spdif = ""
                else:
                    self._player.audio_channels = channels
                    # Downmix normalization only matters when we are the ones
                    # downmixing, which is exactly the stereo case.
                    self._player.audio_normalize_downmix = mode == "stereo"
                    self._player.audio_spdif = ",".join(codecs)
                self._set_af(AF_NIGHT_MODE, NIGHT_MODE_FILTER if night else None)
                # The AC3 encoder is re-decided per track; drop any stale one
                # so a mode change out of optical takes effect without a
                # reload.
                if mode != "optical":
                    self._set_af(AF_AC3_ENCODE, None)
                else:
                    self._apply_audio_filters_locked()
                log.info(
                    "Audio config - mode: %s, channels: %s, passthrough: %s, "
                    "night mode: %s",
                    mode,
                    channels or "restored",
                    ",".join(codecs) or "none",
                    "on" if night else "off",
                )
            except _mpv_errors:
                raise
            except Exception:
                log.error("Could not apply audio settings.", exc_info=True)

    def apply_audio_filters(self):
        """Decide the AC3 encoder for the track that is playing now.

        Runs on every file load *and* every audio-track change: the choice
        depends on the selected track's codec, so switching from an AC3 track
        to a 5.1 AAC one has to re-decide or the surround is lost.
        """
        if self._player is None:
            return
        with self._audio_lock:
            try:
                self._apply_audio_filters_locked()
            except _mpv_errors:
                raise
            except Exception:
                log.error("Could not apply audio filters.", exc_info=True)

    def _apply_audio_filters_locked(self):
        """apply_audio_filters' body; caller holds ``_audio_lock``.

        Optical only, and only for tracks we are *not* passing through --
        handing mpv audio-spdif and lavcac3enc for one track builds a chain
        it cannot satisfy, and it recovers by disabling the filter, so the
        encoder would simply stop working with nothing to show for it.
        """
        mode = settings.audio_mode or "auto"
        if mode != "optical":
            return
        codecs = audio_spdif_codecs(mode, bool(settings.audio_night_mode))
        track_codec = self._mpv_property("current-tracks/audio/codec")
        want = audio_wants_ac3_encode(
            mode, track_codec, codecs,
            bool(settings.audio_optical_encode_ac3),
            bool(settings.audio_passthrough_ac3))
        self._set_af(AF_AC3_ENCODE, AC3_ENCODE_FILTER if want else None)

    def set_night_mode(self, enabled: bool):
        """Toggle night mode and apply it live (no reload needed)."""
        settings.audio_night_mode = bool(enabled)
        settings.save()
        self.apply_audio_settings()

    def _notify_mpv_gone(self):
        handler = self.on_mpv_gone
        if handler is None:
            return
        try:
            handler()
        except Exception:
            log.error("on_mpv_gone handler failed", exc_info=True)

    def _notify_mpv_terminated(self):
        """mpv is really dead — see on_mpv_terminated. Runs on the terminate
        thread, so handlers must not block it for long."""
        handler = self.on_mpv_terminated
        if handler is None:
            return
        try:
            handler()
        except Exception:
            log.error("on_mpv_terminated handler failed", exc_info=True)

    # End-of-playback choreography shared by the eof and abort observers:
    # arm the pause-swallow, take the dedup lock non-blockingly (whichever
    # observer fires first wins), and stamp the task with the playback epoch
    # so it no-ops if a new file starts before it runs.
    def _queue_finished(self):
        self.pause_ignore = True
        has_lock = self._finished_lock.acquire(False)
        self.put_task(self.finished_callback, has_lock, self._play_epoch)

    def run_action(self, func):
        """Run a UI-originated player action without ever blocking the caller.

        ``_lock`` is held for the whole of a playback start — the mpv load
        plus the duration wait, which is bounded only by playback_timeout
        (30s by default) and is routinely the full timeout when a stream
        fails to open. UI actions run inline on the browser's loop thread, so
        calling a @synchronous method straight through froze the entire
        window for that whole stretch: the loading screen painted, and then
        the first press of pause/seek/stop wedged it.

        Fast path is unchanged — if the lock is free the action runs inline
        and synchronously, so normal transport control keeps its exact
        current behaviour. Only when something else holds the lock does the
        action defer onto the action thread, applying once that work
        finishes. ``func`` takes the PlayerManager.
        """
        if self._lock.acquire(blocking=False):
            try:
                return func(self)
            finally:
                self._lock.release()
        # Almost always a playback start in progress. Deferring beats both
        # blocking the caller and dropping the user's input.
        log.debug("Player is busy; deferring UI action to the action thread.")
        self.put_task(func, self)
        return None

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
    def _pump_trickplay(self):
        """Start the trickplay tile fetch once playback is actually running.

        Deferred off the playback-start path on purpose: the fetch is dozens
        of serial HTTP requests to the same host mpv is streaming from, and
        issuing them while the demuxer is still opening the file competed for
        connections with the open itself.

        "core-idle false" is the signal — mpv is decoding and presenting
        frames, so the open is done and the demuxer has what it needs. Falls
        back to a positive playback_time for backends that don't expose
        core-idle. Runs on the action thread, once per playback.
        """
        if not self._trickplay_pending or self.trickplay is None:
            return
        try:
            idle = self._player.core_idle
            if idle is None:
                live = (self._player.playback_time or 0) > 0
            else:
                live = not idle
        except _mpv_errors:
            return          # mpv went away; the next play re-arms this
        except Exception:
            log.debug("Could not read playback state for trickplay.",
                      exc_info=True)
            return
        if not live:
            return
        self._trickplay_pending = False
        log.debug("Playback is live; starting the trickplay fetch.")
        self.trickplay.fetch_thumbnails()

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
        self._pump_trickplay()
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

                # With the HUD, "ask" mode shows the Skip Intro/Credits
                # button (scene button while summoned, standalone
                # overlay while idle) instead of the seek-to-skip OSD
                # text prompt; _hud_skip carries the live segment.
                hud_skip_button = (
                    getattr(self, "_osc_style_resolved", None) == "mpvtk"
                    and self.mpvtk_active
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

                    if hud_skip_button:
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
                elif self._check_stalled_finish(video):
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
        # A start begins HERE, not when mpv is handed the url: resolving it is
        # a PlaybackInfo round trip, and the UI has had a spinner up since the
        # click. Cancel used to be gated on _loading, which only covers the
        # mpv-side wait, so cancelling during this round trip was silently
        # dropped and the video played anyway.
        self._load_cancelled = False
        self._start_in_progress = True
        try:
            url = video.get_playback_url()
            if not url:
                log.error("PlayerManager::play no URL found")
                return
            self._play_media(video, url, offset, no_initial_timeline,
                             is_initial_play, apply_memory)
        finally:
            self._start_in_progress = False

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
        # A start supersedes any browse background a previous one deferred.
        self._browse_bg_deferred = False
        self.menu.hide_menu()

        if self.trickplay:
            self.trickplay.clear()

        if settings.log_decisions:
            log.info("Playing: {0}".format(url))
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
        # Arm load-failure detection before play(): mpv can report the file
        # unloadable before the duration wait below even starts.
        self._load_generation += 1
        self._load_failed.clear()
        self._load_completed.clear()
        self._load_error_detail = None
        # Keep the armed geometry equal to the window's live size, so X11's
        # re-apply on the coming VO reconfig lands on the size the user has.
        self._sync_window_geometry()
        clear_mpv_errors()
        self._loading = True
        # Tell the UI a load is in flight. Until this existed the window just
        # went blank for however long the load took (up to playback_timeout),
        # with nothing to distinguish "still loading" from "silently failed".
        self._notify_load_start(video)
        try:
            if self._load_cancelled:
                # Cancelled while the url was still being resolved — don't
                # hand mpv a file only to stop it a moment later.
                loaded = False
            else:
                self._player.play(self.url)
                loaded = wait_property(
                    self._player,
                    "duration",
                    lambda x: x is not None,
                    settings.playback_timeout,
                    skip_initial=True,
                    abort=self._load_failed,
                    satisfied_by=self._load_completed,
                )
        finally:
            self._loading = False
        if not loaded:
            cancelled, self._load_cancelled = self._load_cancelled, False
            if cancelled:
                # The user abandoned the start. Nothing to report and nothing
                # to retry — they already moved on.
                log.info("Playback start cancelled.")
                self._failed_playback = None
                self._abort_load()      # stop() alone would not; see there
                self.stop()
                return
            # Two distinct failures: mpv said the file is unloadable (fast,
            # with a cause), or nothing arrived within playback_timeout.
            errored = self._load_failed.is_set()
            detail = self._load_error_detail or last_mpv_error()
            if errored:
                log.error("Could not load media: %s", detail or "unknown error")
            else:
                log.error("Timeout when waiting for media duration. Stopping playback!")
            # Stash before stop(): the retry offered below replays this exact
            # video, and stop() is what clears the rest of the play state.
            self._failed_playback = (video, offset)
            # BEFORE stop(), which pushes a stopped playstate that sends the
            # browser back to the library. Reporting after it meant the UI had
            # already returned to browse by the time the error arrived, so it
            # was classified as a non-playback failure and downgraded to a
            # toast — a failed load looked like an unexplained bounce back to
            # the library.
            self._notify_load_error(video, detail, timed_out=not errored)
            # Before stop(), which early-returns here: the half-open file has
            # to be dropped or it keeps loading and eventually plays itself.
            self._abort_load()
            self.stop()
            return
        log.info("Finished waiting for media duration.")
        if self._load_cancelled:
            # Cancelled in the gap between duration arriving and the start
            # finishing. Small window, but without this check the cancel is
            # swallowed and the video the user just dismissed plays anyway.
            log.info("Playback start cancelled just as it completed.")
            self._load_cancelled = False
            self._failed_playback = None
            self._abort_load()
            self.stop()
            return
        # A start that got this far succeeded; nothing is left to retry.
        self._failed_playback = None
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
        # Likewise the stall window: a position carried over from the previous
        # file would otherwise be compared against the new one's timeline.
        self._stall_position = None
        self._stall_since = 0.0
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

        if self.syncplay.is_enabled():
            self.set_speed(1)
            self.syncplay.play_done()
        else:
            self.set_paused(False, False)

        # Trickplay (scrubbing thumbnails) is video-only — skip the fetch for
        # audio so switching songs isn't slowed by a pointless request.
        #
        # Armed, not fired: the fetch pulls dozens of tile JPEGs serially from
        # the same host mpv is streaming from, and duration arrives while the
        # demuxer is still seeking around the file opening fresh connections
        # per seek. Racing those against each other starved the stream open —
        # the field symptom being intermittent TLS errors and opens dragging
        # out to tens of seconds. update() fires this once playback is
        # genuinely live (see _pump_trickplay).
        self._trickplay_pending = bool(self.trickplay and not v_audio)

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
        if not value:
            # Releasing force_window IS the minimize — mpv destroys the
            # window rather than the WM iconifying it — so this is the last
            # moment its size can be read. Persist it for the next launch and
            # note it for the next window, which is what makes raising from
            # the tray come back where the user left it.
            self._save_window_geometry()
            size = self._reopen_window_size()
        self._player.force_window = value
        if not value:
            # Only once the window is gone. Writing geometry while it still
            # exists is a resize *command* (see _sync_window_geometry), and on
            # Windows and X11 that write also forces mpv's own
            # window-maximized option to false — which is the flag that
            # re-maximizes the window when it comes back.
            self._rearm_window_geometry(size)

    def _reopen_window_size(self):
        """The size the next window should open at, read while this one lives.

        Separate from _save_window_geometry, which persists to settings and
        is gated on remember_window_size: reopening from the tray mid-session
        should land where the user left the window whether or not they asked
        for it to be remembered across launches. Falls back to the configured
        size when the live size is unreadable or the window is maximized.
        """
        width = height = 0
        try:
            if not self._player.window_maximized:
                width = int(self._player.osd_width or 0)
                height = int(self._player.osd_height or 0)
        except Exception:
            pass
        if width < 320 or height < 240:
            width = max(320, int(settings.window_width or 1280))
            height = max(240, int(settings.window_height or 720))
        return width, height

    def _sync_window_geometry(self):
        """Re-arm the geometry option at the window's *current* size.

        The obvious move here — clearing geometry so it cannot be re-applied —
        is what caused the window to jump on playback. Writing the option at
        runtime is not bookkeeping: every VO treats it as a resize command
        (w32_common and x11_common un-maximize the window first and force a
        reset to the computed size; wayland_common calls set_geometry with
        resize=true). With the option cleared, that computed size is whatever
        mpv currently has, i.e. the video's native size — or 960x540 while the
        browser is idle, since that is the dummy size mpv gives a forced
        window. So a maximized window un-maximized itself and shrank to the
        video, and playing from an empty browser landed on mpv's default size.

        Arming the live size instead keeps X11's re-apply on reconfig a no-op
        without ever commanding a resize. While maximized or fullscreen there
        is no floating size to record, and the write itself would un-maximize,
        so leave the last armed value alone.
        """
        try:
            if self._player.fullscreen or self._player.window_maximized:
                return
            width = int(self._player.osd_width or 0)
            height = int(self._player.osd_height or 0)
        except Exception:
            log.debug("Could not read the window size", exc_info=True)
            return
        if width < 320 or height < 240:
            # Torn down, or not mapped yet: a nonsense size is worse than the
            # one already armed.
            return
        self._rearm_window_geometry((width, height))

    def _rearm_window_geometry(self, size):
        """Write the geometry option, if it isn't already what we want.

        Skipping the redundant write matters: see _sync_window_geometry for
        what a write does to a live window.
        """
        want = "%dx%d" % size
        if want == self._geometry_armed:
            return
        try:
            self._player.geometry = want
            self._geometry_armed = want
        except Exception:
            log.debug("Could not re-arm the window geometry", exc_info=True)

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
        it back (the 'q' key while the in-window browser is up). push_playstate(stopped)
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
    def set_volume(self, pct: float, notify: bool = True):
        """Set the player volume.

        ``notify=False`` skips the timeline wake and the bar push. Dragging
        a volume slider produces a value per mouse-move, and each one of
        those was waking the timeline thread — which posts progress to the
        *server*. A single drag across the bar meant a burst of round trips
        for a setting the server does not even track. The UI sets the volume
        live for audible feedback and notifies once, on release.
        """
        if not self._player.playback_abort:
            self._player.volume = pct
        if notify:
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

    @staticmethod
    def _safe_title(video):
        try:
            return video.get_proper_title()
        except Exception:
            return ""

    def _notify_load_start(self, video):
        """Tell the UI a load is in flight. Best-effort and never fatal — a
        broken UI hook must not stop playback from starting."""
        cb = self.on_load_start
        if cb is None:
            return
        try:
            cb({"title": self._safe_title(video)})
        except Exception:
            log.error("on_load_start handler failed.", exc_info=True)

    def _notify_load_error(self, video, detail, timed_out: bool):
        """Report a failed start to the UI, with what a retry could change.

        ``can_transcode`` gates the "retry with transcode" option: it's only
        worth offering when we did NOT already transcode, since re-requesting
        the same transcode would just fail the same way.
        """
        cb = self.on_load_error
        if cb is None:
            return
        try:
            already_transcoding = bool(getattr(video, "is_transcode", False))
            cb({
                "title": self._safe_title(video),
                "detail": detail,
                "timed_out": timed_out,
                "can_transcode": not already_transcoding,
            })
        except Exception:
            log.error("on_load_error handler failed.", exc_info=True)

    def _abort_load(self):
        """Tell mpv to drop a start that never completed.

        stop() cannot do this: it is written for stopping a PLAYING item and
        early-returns on `not self._video` — and _video is only assigned once
        the duration wait SUCCEEDS. So on a cancelled or failed start it
        returned without ever issuing `stop` to mpv, leaving the file to
        finish loading and start playing on its own. With the browse window's
        force_window/keep_open already applied, that surfaced as the video
        playing *behind the library*.
        """
        if not self._mpv_alive:
            return
        try:
            self._player.command("stop")
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        except Exception:
            log.debug("Could not stop mpv after an aborted start.",
                      exc_info=True)
            return
        # mpv has nothing loaded now, which is exactly the state a browse
        # window deferred while this start was in flight. Record it rather
        # than issuing that stop a second time, so the flag matches the
        # window instead of drifting out of sync with it.
        if self._browse_bg_deferred:
            self._browse_bg_deferred = False
            self._showing_browse_bg = True

    def cancel_load(self):
        """Abandon a playback start that is still in flight.

        Reuses the abort the end-file handler sets, so the duration wait
        gives up within a poll interval instead of running out
        playback_timeout — which is the whole point, since the case worth
        cancelling is the one where mpv sits on a stalled stream for 30s.
        The cancelled flag keeps the failure path silent: the user asked for
        this, so there is nothing to report and nothing to retry.

        Deliberately takes no lock, so it is safe to call straight from the
        UI thread while _play_media holds one. Returns whether a start was
        actually in flight.

        Gated on _start_in_progress rather than _loading: _loading only covers
        the mpv-side duration wait, but the start begins one PlaybackInfo
        round trip earlier — and the UI has shown a spinner (with this Cancel
        on it) since the click. Gating on _loading made the button do nothing
        for that whole window.
        """
        if not self._start_in_progress:
            return False
        log.info("Cancelling playback start.")
        self._load_cancelled = True
        self._load_failed.set()
        return True

    def retry_failed_playback(self, force_transcode: bool = False):
        """Re-attempt the start that just failed. Returns whether one was queued.

        Safe to call from the UI thread: the replay is queued onto the action
        thread rather than run here, because play() takes _lock and blocks for
        the whole load — doing that on the browser's loop thread would freeze
        the very dialog the user just clicked.
        """
        failed = self._failed_playback
        if failed is None:
            log.warning("Retry requested with no failed playback to retry.")
            return False
        video, offset = failed
        self._failed_playback = None
        if force_transcode:
            try:
                # Forces the server to transcode instead of direct streaming:
                # the usual fix when the source plays on the server but not
                # over the wire to us.
                video.set_trs_override(None, True)
            except Exception:
                log.error("Could not force transcode for retry.", exc_info=True)
        # apply_memory=False: this is a re-attempt of one specific item, so
        # keep the tracks it was already resolved with.
        self.put_task(lambda: self.play(video, offset, apply_memory=False))
        return True

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

        self._apply_secondary_subtitle()

    def _apply_secondary_subtitle(self):
        """Push the video's secondary-subtitle choice onto mpv's secondary-sid.

        Purely client-side: mpv renders it above the primary track, so it only
        applies to subtitles mpv has itself (embedded text, or an external file
        it can fetch) — a burn-in/transcode track can never be a secondary. The
        same track can't be shown twice, so a secondary that matches the primary
        is treated as off. Not @synchronous — always called under _lock."""
        video = self._video
        if video is None:
            return
        sec = getattr(video, "secondary_sid", None)
        track = None
        if sec is not None and sec != -1 and sec != video.sid:
            if sec in video.subtitle_seq:
                track = video.subtitle_seq[sec]
            elif sec in video.subtitle_url:
                track = self._ensure_external_sub(sec)
        try:
            self._player.secondary_sid = track if track is not None else "no"
        except _mpv_errors:
            self._handle_mpv_disconnect()
        except Exception:
            log.warning("PlayerManager::could not set secondary subtitle",
                        exc_info=True)

    def _ensure_external_sub(self, sub_id: int):
        """mpv track id for an external subtitle, loading it if needed WITHOUT
        disturbing the primary selection (sub_add ``auto``, unlike
        load_external_sub's implicit select). Returns None if it can't load."""
        if sub_id in self.external_subtitles:
            return self.external_subtitles[sub_id]
        try:
            sub_url = self._video.subtitle_url[sub_id]
        except (KeyError, AttributeError):
            return None
        try:
            self._player.sub_add(sub_url, "auto")
        except (SystemError,) + _mpv_errors:
            log.info("PlayerManager::could not load external secondary subtitle")
            return None
        track = self._external_track_id(sub_url)
        if track is not None:
            self.external_subtitles[sub_id] = track
            self.external_subtitles_rev[track] = sub_id
        return track

    def _external_track_id(self, sub_url: str):
        """The mpv track id of a just-added external subtitle, matched back by
        the filename it was added with (sub_add doesn't return the id on either
        backend)."""
        try:
            tracks = self._player.track_list or []
        except Exception:
            return None
        for tr in tracks:
            if (tr.get("type") == "sub"
                    and tr.get("external-filename") == sub_url):
                return tr.get("id")
        return None

    @synchronous("_lock")
    def set_secondary_subtitle(self, sub_uid: int):
        """Select (or, with -1/None, clear) the secondary subtitle track."""
        video = self._video
        if not video:
            return
        video.secondary_sid = None if sub_uid is None or sub_uid == -1 else sub_uid
        self._apply_secondary_subtitle()
        self.timeline_handle()

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
        # (The HUD re-reads osc_bridge.build_state on its next repaint,
        # so track changes show up there without a push.)
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
            ranges = None
            try:
                cache = self._player.demuxer_cache_state
                if cache:
                    ranges = [
                        [float(r["start"]), float(r["end"])]
                        for r in cache.get("seekable-ranges") or []
                    ]
            except Exception:
                ranges = None
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
                # Which queue entry this is, so the browser's queue view
                # can move its now-playing highlight without refetching.
                "id": getattr(video, "item_id", None),
                # Which server it came from. The headless cast screen fetches
                # the playing item to show it, and defaulting to the
                # browser's *selected* server would fetch the wrong thing —
                # or nothing — whenever they differ.
                "server_uuid": _server_uuid_of(video),
                "title": item.get("Name") or "",
                # Where an episode came from. The title alone is a lot less
                # useful than it looks ("Pilot", "Part One"), so the video
                # HUD shows these above it — the audio bar has its own
                # artist/album lines and ignores them. Raw fields, not a
                # formatted string: the view decides how to lay them out.
                #
                # Only for a real Episode: ParentIndexNumber/IndexNumber are
                # generic ordinals. A MusicVideo carries disc and track there
                # and is MediaType Video, so it reaches the HUD — and would
                # have been labelled "S1E3".
                "series_name": (item.get("SeriesName") or ""
                                if item.get("Type") == "Episode" else ""),
                "season": (item.get("ParentIndexNumber")
                           if item.get("Type") == "Episode" else None),
                "episode": (item.get("IndexNumber")
                            if item.get("Type") == "Episode" else None),
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
                # buffered/seekable ranges in seconds, for the HUD's
                # seek-bar shading (None when the demuxer has none)
                "ranges": ranges,
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
        #
        # On the shared reporter rather than its own thread: the stop for the
        # outgoing track is queued just before this, and the server blanks the
        # session on a stop, so a start that overtook it would be erased.
        self._session_ready.clear()
        self._reporter.submit(
            lambda: self._session_playing_safe(video.client, options),
            "session_playing")

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
            # Queued, not called here: this runs on the advance path, and the
            # round trip used to sit between the last sample of one track and
            # the first of the next. Ordering against the following
            # session_playing is what the shared worker guarantees.
            self._reporter.submit(
                lambda: client.jellyfin.session_stop(options), "session_stop")

        if discord_presence:
            try:
                clear_presence()
            except Exception:
                log.error("Could not clear Discord Rich Presence.", exc_info=True)

    def upd_player_hide(self):
        video = self._video
        if video:
            self._player.keep_open = video.parent.has_next

    # How long playback has to sit at an unchanged position, at the end of the
    # media, before the watchdog calls it finished. Long enough not to fire on
    # ordinary rebuffering; short enough that a user does not give up first.
    STALL_FINISH_SECS = 20

    def _check_stalled_finish(self, video):
        """Whether playback has silently died at the end of a remote stream.

        The observers and the poll rescue all wait for mpv to *say* the file
        ended. A remote origin that stops delivering without closing the
        connection produces no such statement: the demuxer blocks in read, so
        there is no end-file event, eof-reached stays False and playback-abort
        stays False. With keep_open holding the last frame mid-queue, that is
        indistinguishable from a normal hold — the queue just stops forever.
        Reported against .strm items, whose origins are arbitrary third-party
        servers, but nothing here is .strm-specific.

        Deliberately requires the position to be at the END of the media, not
        merely frozen. A bare stall is far more likely to be rebuffering on a
        slow origin, and advancing through that would silently skip the rest
        of an episode — a worse outcome than the freeze this fixes. Items with
        no known duration therefore get no rescue; _finished_at_eof cannot
        place them, and guessing is not worth the risk of skipping content.
        """
        # Live streams have no end to arrive at: a stall there is an outage,
        # and "finishing" one would advance the queue past a channel the user
        # is still watching.
        if (video.media_source or {}).get("IsInfiniteStream"):
            return False
        try:
            if self._player.pause:
                return False
            position = self._player.playback_time
        except Exception:
            # Including a disconnect: the eof-reached read just above already
            # owns that case, so by here the connection was alive a moment ago
            # and an unreadable property is not worth a second teardown path.
            return False
        if position is None:
            return False

        now = time.time()
        if position != self._stall_position:
            self._stall_position = position
            self._stall_since = now
            return False
        if now - self._stall_since < self.STALL_FINISH_SECS:
            return False
        if not self._finished_at_eof(video, position):
            return False
        log.warning(
            "Playback stalled at %.1fs at the end of the media without an "
            "end-of-file from mpv; treating as finished.", position
        )
        self._reached_eof = True
        return True

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
    def idle_quit(self, reason="Idle timeout reached"):
        """Quit mpv while idle to free the window / GPU context / memory
        (opt-in via mpv_idle_quit). Re-created on the next play. Gated hard so
        it never fires while anything still needs the window.

        Also the minimize path on an mpv that cannot drop force-window at
        runtime, which is what ``reason`` distinguishes in the log."""
        if not self._mpv_alive or self._video is not None:
            return
        if self.menu.is_menu_shown or self.syncplay.is_enabled():
            return
        if self.mpvtk_active:
            # The in-window browser is on screen; keep the window alive. Note
            # this is cleared when the browser minimizes, so a minimized app
            # *does* idle-quit — which is most of the point of minimizing.
            return
        if is_using_ext_mpv and not settings.mpv_ext_start:
            # Never kill an mpv the user launched themselves.
            return
        log.info("%s; quitting mpv to save resources.", reason)
        self._idle_quit = True
        self.should_send_timeline = False
        player = self._player
        self._teardown_player()
        self._mpv_alive = False
        self._terminate_thread = Thread(
            target=self._terminate_mpv, args=(player,), daemon=True
        )
        self._terminate_thread.start()
        # The handle is no longer ours: let attached UIs stop pushing to it.
        # NOT the point at which they may free buffers mpv reads by address —
        # terminate is still running on the thread above. That is
        # on_mpv_terminated, fired at the end of _terminate_mpv.
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
        # Now — and not before — it is safe to release buffers mpv was
        # reading by address.
        self._notify_mpv_terminated()

    def _save_window_geometry(self):
        """Remember the window size for the next launch.

        Size only: mpv exposes the *actual* window size as osd-width/height,
        but never its position — the `geometry` property reads back the
        option we set, not where the user dragged the window. So there is
        nothing to persist for position without platform-specific code.

        Never raises: this runs on the shutdown path, where mpv may already
        be gone, and failing to remember a window size must not stop the
        app exiting.
        """
        if not settings.remember_window_size:
            return
        try:
            maximized = bool(self._player.window_maximized)
            # A maximized window reports the maximized size; storing that
            # would make un-maximizing later restore to full-screen-ish
            # dimensions. Keep the last floating size and the flag instead.
            if not maximized:
                width = int(self._player.osd_width or 0)
                height = int(self._player.osd_height or 0)
                # 0 while the window is being torn down, and a nonsense
                # size is worse than the default we would otherwise use.
                if width >= 320 and height >= 240:
                    settings.window_width = width
                    settings.window_height = height
            settings.window_maximized = maximized
            settings.save()
        except Exception:
            log.debug("Could not save the window geometry", exc_info=True)

    def terminate(self):
        # Before stop(): stopping can tear the window down, and the size has
        # to be read while it still exists.
        self._save_window_geometry()
        self.stop()
        # After stop(), which is what queues the final report, and outside
        # _tl_lock (the worker takes it). The worker is a daemon, so without
        # this the last stop would be lost to interpreter exit and the server
        # would keep showing the session as playing.
        self._reporter.stop()
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
                        enabled
                        and getattr(self, "_osc_style_resolved", None)
                        != "mpvtk"
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
        release_failed = False
        try:
            if enabled:
                try:
                    # A UI window resizes freely; keeping the aspect would
                    # snap it to whatever was last played. Restored by
                    # browse_yield() for real video.
                    self._player.keepaspect = False
                except Exception:
                    pass  # older mpv without the property; harmless
                # Paint the window ourselves rather than decoding a file to
                # hold it open. This also fixes audio: playing a song
                # replaces whatever is loaded with a picture-less file, and
                # mpv then paints its own background there — black against
                # the UI's dark grey. These are global vo options, so they
                # survive the file change.
                self._player.background = "color"
                self._player.background_color = BROWSE_BG_HEX
                self._player.force_window = True
                self._player.keep_open = True
                self._player.image_display_duration = "inf"
                # force_window with nothing loaded is an empty window
                # painted with background-color — no decode, and none of
                # the video-output teardown that reloading a background
                # file causes (which read as the window closing and
                # reopening). Only when nothing is playing: audio keeps the
                # browser up and must not be stopped out from under itself.
                # Idempotent — stopping playback reaches here twice, once
                # from the stopped-playstate callback and once from the
                # caller.
                #
                # `_loading` is part of the guard, not decoration. _video is
                # only assigned once the duration wait SUCCEEDS, so during a
                # start it is still None while mpv is mid-open — and
                # _play_media clears _showing_browse_bg before playing. Both
                # guards therefore passed, and any path that re-entered browse
                # during a load fired `stop` straight into the open. mpv
                # reported it as "Opening failed or was aborted" + "finished
                # playback, success (reason 2)", with ffmpeg logging "tls:
                # Error decoding the received TLS packet" as the socket was
                # torn down mid-read — which is what made these look like
                # random TLS/network faults rather than us aborting our own
                # playback.
                if self._video is None and not self._showing_browse_bg:
                    if self._loading:
                        # Deferred, not skipped. Stopping here is what used to
                        # abort our own in-flight open; but simply dropping it
                        # left the flag saying "no browse background" while
                        # the window went on to show one, so the state no
                        # longer described the window. _abort_load applies
                        # this if the start does not end up playing.
                        self._browse_bg_deferred = True
                    else:
                        self._player.command("stop")
                        self._showing_browse_bg = True
                        self._browse_bg_deferred = False
                # Browsing is a desktop-UI activity: only go fullscreen if the
                # user explicitly asked for a fullscreen browser. settings.
                # fullscreen still applies when playback starts.
                #
                # headless is a kiosk: the cast screen IS the display, so it
                # stays fullscreen throughout. Without this, stopping
                # playback dropped a cast-target box back to a window
                # whenever browser_fullscreen was off — and browser_
                # fullscreen is about the library, which headless does not
                # even have.
                if settings.browser_fullscreen or settings.headless:
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
                # Same _loading guard as the enable branch above: _video is
                # not set until the duration wait succeeds, so without it
                # leaving browse mode during a start would drop force_window
                # and `stop` the open that is still in flight.
                if not self._video and not self._loading:
                    self._set_force_window(False)
                    self._player.command("stop")
                    release_failed = not self._runtime_force_window
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        if release_failed:
            # This mpv decided about its window at startup and will not
            # revisit it (see runtime_force_window_works), so the window we
            # were just asked to give up is still on screen. Quitting mpv
            # *is* the release here. That is only what the idle timer would
            # do a few minutes later anyway, and it costs nothing the app
            # needs: it stays a cast target, and the next play or tray
            # reopen builds a fresh mpv that asks for its window on the
            # command line.
            log.info("Releasing the window by quitting mpv (this mpv "
                     "cannot drop force-window at runtime).")
            self.idle_quit(reason="minimized")

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
        the stretched aspect, the browse background and the non-fullscreen
        browse window. It must not touch force_window/keep_open: playback is
        still starting up here and tearing the window down would kill it."""
        if not self._mpv_alive:
            return
        self._showing_browse_bg = False
        try:
            self._player.keepaspect = True
            if settings.fullscreen and not self.fullscreen_disable:
                self._player.fs = True
            try:
                # set_browse_window() paints the window in the UI's dark grey,
                # and these are global vo options that outlive the file change
                # — so without this, video plays with #141414 letterbox bars
                # instead of mpv's black for the rest of the process's life.
                # Audio keeps the browser up and never reaches browse_yield,
                # so music still gets the UI-matching background.
                self._player.background = MPV_DEFAULT_BACKGROUND
                self._player.background_color = MPV_DEFAULT_BACKGROUND_HEX
            except Exception:
                pass  # older mpv where background is the color option
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
        elif action in self._NAV_KEYPRESS and self._mpvtk_input_active():
            # remote drives the UI's spatial navigation
            try:
                self._player.command(
                    "keypress", self._NAV_KEYPRESS[action])
            except Exception:
                log.debug("nav keypress failed", exc_info=True)
        elif (action in ("up", "down", "left", "right", "ok")
              and self._mpvtk_hud_idle()):
            # Hidden HUD: remote Move*/Select wake it via a script
            # message, NOT keypresses — the idle renderer only grabs
            # the configured wake key, so a keypress would fall through
            # to mpv defaults. Select also toggles pause/play (and
            # accepts a showing skip button). Back keeps its
            # stop-to-browser meaning while hidden.
            try:
                self.script_message(
                    "mpvtk-hud-summon",
                    "select" if action == "ok" else "nav")
            except Exception:
                log.debug("hud summon failed", exc_info=True)
        else:
            # No in-window UI (CLI / Tk / mid-playback): "settings" keeps its
            # historical meaning of opening the OSD menu, which is the only
            # settings surface those paths have.
            self.kb_seek(self._MENU_ALIAS.get(action, action))


playerManager = PlayerManager()
