# ---------------------------------------------------------------------------
# BEFORE EDITING THIS FILE, READ docs/mpv-backends.md.
#
# It carries the constraints that have no line to sit on -- the danger is in
# the line you are about to add, so no inline comment can warn you:
#
#   * mpv is NOT re-created between queue items, so any global option written
#     for one item is still set for the next, including a next item we
#     deliberately refuse to set it for;
#   * a bound method cannot be a libmpv ``property_observer`` (use _observe);
#   * an input section without ``allow-hide-cursor`` stops the mouse cursor
#     ever hiding again;
#   * the two backends raise different things and answer property reads at
#     wildly different cost.
# ---------------------------------------------------------------------------

import logging
import os
import re
import sys
import time
import json

from threading import RLock, Lock, Thread, Event
from queue import Queue, Empty as queue_empty
from collections import deque
from typing import TYPE_CHECKING, Optional

from . import conffile
import threading

from .utils import same_origin, synchronous, Timer, get_resource
from .media import segment_labels
from .mpv_events import observe as observe_property
from .mpv_events import wait_property
from .player_audio import AudioMixin
from .player_reporting import ReportingMixin
from . import player_window
from .player_window import WindowMixin, wlog
from .mpv_options import (REPLACES_OSC, build_mpv_options, mpv_scripts,
                          resolve_osc_style)
from .session_reporter import SessionReporter
from . import conf
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
        # With the exception, not just the fact of it. "Could not find
        # libmpv" is only one of the two things this OSError means, and on
        # the Windows build it is never the right one: libmpv ships *inside*
        # the installer, so what this reports there is that the DLL was
        # found and would not load -- a dependency of its own that the
        # machine does not have. 3.0.0pre12 shipped exactly that (a libmpv
        # hard-linked against vulkan-1.dll), and the log said only that
        # something was wrong with libmpv, while the traceback that reached
        # the user blamed a missing mpv.exe. The chained cause carries the
        # WinError and the path; it is the whole diagnosis.
        log.warning("Could not load libmpv.", exc_info=True)
        python_mpv_available = False

if settings.mpv_ext or not python_mpv_available:
    import python_mpv_jsonipc as mpv

    log.info("Using external mpv playback backend.")
    is_using_ext_mpv = True


def _disarm_sdl_signal_handlers():
    """Keep SDL's signal handlers out of the *external* mpv's child process.

    mpv's gamepad support is SDL2, and ``SDL_Init`` takes any SIGINT/SIGTERM
    handler still at ``SIG_DFL`` unless this says otherwise. The external
    backend's child mpv has none of its own -- jsonipc spawns it with
    ``terminal=no``, which skips the handler install -- so SDL takes its
    SIGTERM and ``MPVProcess.stop()`` is swallowed, orphaning the window.

    **Called unconditionally.** ``input-gamepad`` is an ordinary mpv option,
    so the user's own ``mpv.conf`` can start the SDL thread with the option
    absent from anything we built; there is no way to ask mpv in time, so the
    only correct gate is no gate. A blank value counts as unset
    (``SDL_GetHintBoolean`` returns the default for ``""``); ``"0"`` is
    honoured and logged.

    Why this is the layer under ``mpv_shim._claim_sigterm`` rather than the
    fix, and the process-wide ``os.environ`` side effect that was accepted to
    get it: docs/mpv-backends.md section 4.
    """
    if os.environ.get("SDL_NO_SIGNAL_HANDLERS"):
        if os.environ["SDL_NO_SIGNAL_HANDLERS"] not in ("1", "true", "yes"):
            log.warning(
                "SDL_NO_SIGNAL_HANDLERS=%r in the environment, so SDL keeps "
                "its own signal handlers. With game controller input and an "
                "external mpv, that means this application cannot stop its "
                "mpv process on the way out.",
                os.environ["SDL_NO_SIGNAL_HANDLERS"])
        return
    os.environ["SDL_NO_SIGNAL_HANDLERS"] = "1"


def _rejected_option(error):
    """The mpv option an exception blames, as an mpv_options key, or None.

    Both backends can name it and neither does so the same way -- libmpv puts
    it in an ``AttributeError``'s third arg, jsonipc >= 1.3.0 exposes
    ``bad_option``. (That version is the shim's floor: older ones flattened
    every start failure into "MPV process retry limit reached." after spending
    the whole retry budget.) See docs/mpv-backends.md section 2.

    Returns the *underscored* form, because that is how the option appears in
    the dict handed to ``mpv.MPV``.
    """
    bad = getattr(error, "bad_option", None)
    if isinstance(bad, str) and bad:
        return bad.replace("-", "_")
    args = getattr(error, "args", ())
    if len(args) >= 3 and isinstance(args[2], tuple) and len(args[2]) >= 2:
        name = args[2][1]
        if isinstance(name, bytes):
            return name.decode("utf-8", "replace").replace("-", "_")
        if isinstance(name, str):
            return name.replace("-", "_")
    return None


def _reap_orphaned_core(error):
    """Destroy the mpv core a failed ``mpv.MPV(...)`` left running.

    libmpv only, and not defensive: python-mpv initializes in a ``finally``,
    so an option this build lacks raises *after* the core is up. What is left
    is not a stale handle but a **running mpv** -- window, scripts, threads --
    that nothing stops before the retry's mpv is up, giving two VOs on one
    display and a crash in a thread that is not Python's. Both option retries
    below depend on this reaping. See docs/mpv-backends.md section 2.

    The instance is recovered from the traceback because the constructor never
    returned it, and identified by holding a live handle rather than by class
    (the external backend has no core here to reap). Read out of ``__dict__``
    directly: libmpv's ``__getattr__`` turns an unknown attribute into a
    property read on a core in no state to answer one.
    """
    tb = getattr(error, "__traceback__", None)
    while tb is not None:
        owner = tb.tb_frame.f_locals.get("self")
        handle = getattr(owner, "__dict__", {}).get("handle")
        stop = getattr(owner, "terminate", None)
        if handle is not None and callable(stop):
            try:
                stop()
            except Exception:
                log.debug("could not stop the mpv left by a failed "
                          "construction", exc_info=True)
            return
        tb = tb.tb_next


def _explain_missing_mpv():
    """Say why there is no mpv, before the traceback says something else.

    The external backend starts an mpv binary, so its failure to find one is
    reported by ``subprocess`` as a missing file -- accurate, and misleading
    whenever this backend was not the user's choice. Two different situations
    arrive here and they want opposite advice, and the one thing neither can
    be told from the traceback is which of them it is.
    """
    if not is_using_ext_mpv:
        # Nothing here spawns anything, so this is some other missing file
        # and neither explanation below applies. Say nothing rather than
        # guess -- the traceback is the better answer.
        return
    if settings.mpv_ext:
        log.error(
            "Could not start mpv. The external mpv backend is selected, so "
            "an mpv binary has to be on PATH or named by the mpv_ext_path "
            "setting."
        )
    else:
        # Not the user's doing: libmpv failed to load and the fallback took
        # over silently. On the Windows and Flatpak builds libmpv is what
        # ships, and there is no mpv binary to fall back to, so this is
        # fatal and the libmpv failure is the thing to go and read.
        log.error(
            "Could not start mpv. libmpv would not load (see the earlier "
            "warning for why), and the external mpv backend it fell back to "
            "found no mpv binary to run. This build ships libmpv, so the "
            "earlier warning is the failure worth reporting."
        )


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
# away. Only the external (jsonipc) backend needs it: a command there is a
# request/response over a socket waited for with
# python_mpv_jsonipc.TIMEOUT, which is 120s -- and mpv can accept a
# command, run it, and exit before writing the reply back, parking the
# action thread with the whole shutdown queued behind it. libmpv raises
# immediately on a dead handle. Bounded rather than hunting individual
# calls: during teardown no command's answer is worth minutes.
# See docs/mpv-backends.md section 1.
IPC_TEARDOWN_TIMEOUT = 5

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



def _source_height(video):
    """The video height of what is about to play, or None.

    Read off the **MediaSource**, not the item: an item with several
    versions has one height per version, and the one being played is the
    one that decides whether hardware decoding is worth its risk. A photo,
    an audio track and anything the server did not probe all answer None,
    which every caller treats as "software" -- see mpv_options.hwdec_for.
    """
    try:
        streams = (getattr(video, "media_source", None) or {}).get(
            "MediaStreams") or []
        for stream in streams:
            if stream.get("Type") == "Video" and stream.get("Height"):
                return int(stream["Height"])
    except Exception:
        log.debug("could not read the source height", exc_info=True)
    return None


def runtime_force_window_works(version):
    """Whether this mpv acts on a force-window change made while idle.

    Every mpv *stores* the property; only 0.41+ creates or destroys the video
    output for it. Older builds decide at startup, so the window must be asked
    for on the command line (see ``_init_mpv``) and releasing it later does
    nothing.

    An unreadable version is treated as old: assuming old costs a fallback
    that works everywhere, assuming new costs a window that will not go away.
    Why the browser is the first caller to need this: docs/mpv-backends.md
    section 3.
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


#: Our own pseudo-level: `debug` with the filter below turned off. mpv has no
#: such level, so it is handed `debug` and only our side of the plumbing
#: changes -- see mpv_loglevel_for().
MPV_NOISE = "noise"


def mpv_loglevel_for(level: str) -> str:
    """What to hand mpv. Every value is one of mpv's own except `noise`."""
    return "debug" if level == MPV_NOISE else level


#: mpv lines that are almost entirely us talking to ourselves: the renderer's
#: per-frame scene pushes and metrics (a `mpvtk-scene` line carries the whole
#: serialized UI, so it is enormous as well as constant) and gpu-next's
#: per-frame chatter. At `debug` -- which is the level someone turns on to
#: read one specific thing -- these bury it thousands of lines deep. `noise`
#: is how you ask for them back.
_MPV_NOISE_ARGS = re.compile(
    r'^Run command: script-message, flags=\d+, '
    r'args=\[args="mpvtk-(?:scene|metrics)"')


def _is_mpv_noise(prefix: str, text: str) -> bool:
    if prefix.startswith("vo/gpu-next"):
        return True
    return prefix == "cplayer" and _MPV_NOISE_ARGS.match(text) is not None


def mpv_log_handler(level: str, prefix: str, text: str):
    # Never above info: a real gpu-next failure has to survive the filter, and
    # _recent_mpv_errors below is the only place a failed load can say *why*.
    if (
        level not in ("fatal", "error", "warn")
        and settings.mpv_log_level != MPV_NOISE
        and _is_mpv_noise(prefix, text)
    ):
        return
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




def chapter_target(chapters, pos, direction):
    """Where a previous/next-chapter jump from ``pos`` lands, or None when
    there is nowhere to go.

    ``chapters`` is a list of dicts with a ``time`` in seconds, in order.

    Back restarts the chapter you are in unless you are in its first couple of
    seconds; forward has no grace at all and takes the next boundary strictly
    ahead. Rationale for the asymmetry: docs/mpv-backends.md section 3.

    **The answer is clamped to 0** (#614). A matroska chapter can start at a
    slightly negative timestamp, and mpv reads a negative ABSOLUTE seek as the
    END of the file -- so "previous chapter" hit EOF and the EOF observer
    advanced the queue. ``seek()`` refuses one as well, because the chapter
    PICKER hands it the very same value.

    **Both directions can answer None, and the caller must not seek.** A
    button that quietly restarts the file is worse than one that declines.

    One definition, because two callers jump for different reasons: the HUD's
    chapter buttons and the mouse's back/forward buttons (mouse_chapter_nav).
    """
    if direction < 0:
        target = None
        for ch in chapters:
            if ch["time"] < pos - 2.0:
                target = ch["time"]
        return None if target is None else max(0.0, target)
    for ch in chapters:
        if ch["time"] > pos:
            return ch["time"]
    return None


class PlayerManager(AudioMixin, ReportingMixin, WindowMixin):
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
        # Separate from _audio_snapshot: the device survives a
        # return to "auto" mode, so it cannot share that reset.
        self._device_snapshot = None
        # Serializes the audio settings read + the mpv writes it implies.
        self._audio_lock = RLock()
        self._lock = RLock()
        self._tl_lock = RLock()
        self._finished_lock = Lock()
        self.last_update = Timer()
        self._jf_settings = None
        self.pause_ignore = None  # Used to ignore pause events that come from us.
        #: owner -> semantics, and the sweep, for the #16 key claims.
        self._key_claims = {}
        self._key_actions = {}
        self._swept = None
        self._swept_ptr = None
        #: Whether this mpv can run lua, once asked. See lua_works.
        self._lua_works = None
        self._gamepad_works = None
        #: An OSC style a fallback forced, or None. Survives mpv
        #: re-creation -- see _init_mpv.
        self._osc_style_override = None
        #: Whether we have told the on-screen controls to hide. The latch
        #: half of enable_osc; see there.
        self._osc_suppressed = False
        self._lua_probe = None
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
        # Both built by the first _init_mpv and kept across every later one.
        self.menu = None
        self.syncplay = None
        self.osc_bridge = OscBridge(self)
        self.is_in_intro = False
        self.playback_time_before_seek = None
        # time.time() of the last seek initiated from the jellyfin OSC's
        # own controls (seekbar/buttons); such seeks never intro-skip.
        self._last_ui_seek_time = 0.0
        self.trickplay = None
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
        # Tracks whether mpv's stats.lua ("Playback Data") overlay is up, so
        # it can be cleared when the library comes back. It is ASS OSD, which
        # the in-window browser draws over — but left on, it lingers behind
        # the library once playback ends. The `i` key and the lua OSC's gear
        # sheet funnel through toggle_stats() so this stays truthful (the
        # mpvtk HUD's gear row is now our own Playback Info -- see #10).
        self._stats_shown = False
        # The playback HUD's per-session deinterlace force: True while the
        # gear menu has turned it on for what is being watched, None while
        # `deinterlace_auto` decides. Cleared when the library comes back
        # or the window goes away (clear_deinterlace_override), so it
        # outlives a queue advance -- a badly-flagged season is one answer,
        # not one per episode -- and nothing else.
        self._deinterlace_override = None
        # {mpv property: value} for every property any preset-driven setting
        # can write, as the FRESH mpv had them -- so the restore hands back
        # mpv's defaults plus the user's own mpv.conf, and nothing else.
        # Captured at construction rather than before the first write,
        # because the shader pack writes several of these (deband above all)
        # earlier in the play path than we do; a lazy snapshot would record
        # the pack's values as the user's. See _snapshot_render_pristine.
        self._render_pristine = {}
        # Which of those settings we have actually written. Empty means we
        # have never touched mpv, which is what makes "off" a no-op rather
        # than an override of somebody's mpv.conf -- and what keeps "off"
        # meaning "no opinion" rather than "undo the shader pack".
        # See _apply_render_preset.
        self._render_written = set()
        # Whether this mpv rejected `deinterlace=auto` (it is 0.38+). Asked
        # once; without it an old build re-discovers and re-logs on every
        # item played, forever. Same shape as `_lua_works`.
        self._no_deinterlace_auto = False
        # True while the in-window browser's solid background image is the
        # loaded file. Guards against reloading it on top of itself, which
        # tears the video output down and back up (a visible window
        # close/reopen). Cleared whenever real media takes over.
        self._showing_browse_bg = False
        # Optional callback invoked when the user closes the mpv window while
        # the in-window UI owns it. Set by mpvtk_browser.ui, which decides
        # between minimizing to the tray and quitting. Unset -> stop_and_close.
        self.on_window_closed = None
        # mpv is torn down and re-created across idle-quit and crash
        # recovery, so anything holding the raw handle has to follow it.
        # TWO phases, and the distinction is load-bearing:
        #
        #   on_mpv_gone       - the handle is no longer OURS; stop pushing to
        #                       it. mpv may still be running.
        #   on_mpv_terminated - mpv is actually dead. Only now is it safe to
        #                       free anything mpv reads BY ADDRESS, i.e. the
        #                       in-process BGRA tile buffers -- freeing them
        #                       any earlier is a segfault on quit.
        #
        # on_mpv_recreated fires once a fresh handle is ready.
        # See docs/mpv-backends.md section 8.
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
        # The window gained or lost a server-drawn title bar (set by the UI,
        # which redraws its own chrome to match). On Wayland this is not
        # something we did: mpv writes `border` from what the compositor
        # granted, and that answer can arrive after the window is already up.
        self.on_decorations_changed = None
        # Opens the playback HUD's gear menu (set by mpvtk_browser.ui).
        # During video under the in-window OSC, the kb_menu key routes
        # here instead of the OSD menu. Returns True when handled.
        self.on_hud_menu = None
        # Optional callback (set by the UI) invoked (version, url) when an
        # update is found, so the notice shows in the browser UI instead of on
        # the MPV OSD. Unset for CLI users -> update_check falls back to the OSD.
        self.notify_update = None
        # Called (no args, set by the UI) when SyncPlay is joined or left, so
        # the renderer's HUD token is re-sent: its `syncplay` flag decides
        # whether the renderer's own pause paths hand over to Python, and a
        # group joined mid-playback would otherwise keep the local one.
        self.on_syncplay_change = None
        # Optional callback (set by the UI) invoked with a SyncPlay message, so
        # "N has joined" and friends land on the browser's status line instead
        # of the MPV OSD. Unset -> SyncPlayManager.player_message falls back to
        # the OSD (settings.sync_osd_message).
        self.notify_syncplay = None
        # Optional predicate (set by the UI) answering "if playback stops now,
        # can the user still reach the SyncPlay menu?". Stopping halts group
        # playback rather than leaving the group, which is only tolerable when
        # there is a way back out; unset (CLI) or False -> stop() leaves.
        # See _release_syncplay.
        self.syncplay_menu_reachable = None
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

    def _effective_osc_style(self):
        """The OSC style this mpv should be built with.

        `resolve_osc_style()` answers from settings, which know nothing about
        the machine -- so a fallback that discovered something about *this*
        mpv (no lua, no Pillow) has to be applied on top, and **has to keep
        being applied**: `_init_mpv` runs again on every re-creation, and
        re-resolving from settings there undoes the whole fallback (see
        docs/mpv-backends.md section 8).

        Its answer feeds `mpv_scripts` and `build_mpv_options` too, because
        there is no point handing lua scripts to an mpv known not to run them.
        """
        if self._osc_style_override is not None:
            return self._osc_style_override
        return resolve_osc_style()

    def _construct_mpv(self, mpv_options, **kwargs):
        """``mpv.MPV(...)``, retried without ``--osc`` if mpv has no such
        option -- which means it was built without lua.

        **A build without lua does not ignore ``--osc``, it refuses to
        start**, which made the whole lua fallback unreachable. The two
        backends report entirely different errors and none of it has to be
        parsed, because ``--osc`` is the only lua-gated option the shim sets:
        failing with it and succeeding without it *is* the answer.

        Recorded rather than rediscovered (`lua_works` then skips its probe),
        and **only in this direction** -- mpv having ``--osc`` says lua was
        compiled in, not that it runs. Measurements and the retry-budget cost
        of re-learning: docs/mpv-backends.md section 2.
        """
        from .mpv_options import GAMEPAD_OPTION, OSC_OPTION

        options = dict(mpv_options)

        # Already discovered, on a previous mpv. Re-learning either costs a
        # failed construction every time, and on the external backend a
        # failed construction used to be the whole start-retry budget --
        # measured at ~31s with the shipped defaults, paid on every re-open
        # (idle-quit then a cast, set_browse_window, force_window) before
        # anything plays.
        if self._gamepad_works is False:
            options.pop(GAMEPAD_OPTION, None)
        if self._lua_works is False:
            options.pop(OSC_OPTION, None)

        # Unconditional, and not gated on GAMEPAD_OPTION being in this
        # dict: mpv reads `input-gamepad` from the user's own mpv.conf too,
        # where we cannot see it. See the function.
        _disarm_sdl_signal_handlers()

        dropped_gamepad = False
        dropped_osc = False

        while True:
            try:
                player = mpv.MPV(**kwargs, **options)
            except FileNotFoundError:
                # There is no mpv binary to spawn. Handled ahead of the
                # retries rather than by them, for two reasons: dropping an
                # option cannot conjure one, so the retry only spends the
                # start-retry budget and then reraises the same error; and
                # the error it reraises comes from `subprocess`, which reads
                # as "mpv is not installed" even when nobody was ever asked
                # to install it. That is how the vulkan-1.dll regression
                # presented -- libmpv would not load, this backend was
                # chosen for us, and the traceback named a missing mpv.exe.
                _explain_missing_mpv()
                raise
            except Exception as error:
                _reap_orphaned_core(error)

                # **Which** option was refused, when mpv says so. This used
                # to rest on "there is only one option this can be", and
                # that argument retired the moment a second build-gated
                # option existed. Both backends can be asked -- libmpv puts
                # the name in the exception, python-mpv-jsonipc >= 1.3.0
                # sets `bad_option` -- so a gamepad build gate is identified
                # rather than guessed at.
                if (_rejected_option(error) == GAMEPAD_OPTION
                        and GAMEPAD_OPTION in options):
                    del options[GAMEPAD_OPTION]
                    dropped_gamepad = True
                    continue

                # --osc is not identified by name, deliberately: an mpv
                # built without lua is old enough that the name may not
                # reach us, and it stays the fallback for *any*
                # unexplained failure while it is still present. Dropped
                # once, then a second failure is the real error.
                if OSC_OPTION in options and not dropped_osc:
                    del options[OSC_OPTION]
                    dropped_osc = True
                    continue
                raise

            # Log after success, never before. If the option was not the
            # problem this loop has already reraised, and saying "built
            # without lua" on the way to an unrelated failure would send the
            # next person reading the log somewhere else entirely.
            if dropped_gamepad:
                log.warning("This mpv has no --%s option, so it was built "
                            "without SDL2 gamepad support. Game controller "
                            "input is unavailable; nothing else is "
                            "affected.", GAMEPAD_OPTION.replace("_", "-"))
                self._gamepad_works = False
            if dropped_osc:
                log.warning("This mpv has no --%s option, so it was built "
                            "without lua. The library browser, the playback "
                            "HUD and the on-screen controls all need it; "
                            "falling back to the command line and the OSD "
                            "menu.", OSC_OPTION)
                self._lua_works = False
            return player

    def _init_mpv(self):
        # Re-open reuses this method; drop the previous instance's trickplay
        # thread first so recovery/idle cycles don't leak it.
        # getattr: _player isn't bound until the first init finishes.
        reopen = getattr(self, "_player", None) is not None
        wlog.info("CREATE mpv (%s) <- %s",
                  "re-open" if reopen else "first", player_window._caller())
        self._teardown_player()

        osc_style = self._effective_osc_style()

        trickplay_started = False
        if settings.thumbnail_enable:
            try:
                from .trickplay import TrickPlay

                self.trickplay = TrickPlay(self)
                self.trickplay.start()
                trickplay_started = True
            except Exception:
                log.error("Could not enable trickplay.", exc_info=True)

        # Resolved style for this mpv instance (settings may hold the
        # legacy alias / a fallback may have applied) — the c-menu
        # routing, enable_osc and the skip-button path key off it.
        self._osc_style_resolved = osc_style
        # "mpv" is the one style whose OSC is a lua script of ours. Which
        # script that is belongs to mpv_scripts; this only records that one
        # was loaded, so the built-in OSC can be held off below.
        self._osc_script_loaded = osc_style == "mpv"
        self._osc_suppressed = False

        # ensure standard mpv configuration directories and files exist
        conffile.get_dir(APP_NAME, "scripts")
        conffile.get_dir(APP_NAME, "fonts")
        conffile.get(APP_NAME, "input.conf", True)
        conffile.get(APP_NAME, "mpv.conf", True)

        # Whether the in-window UI should be on screen the moment mpv comes
        # up. First launch honours start_minimized; a re-open (crash
        # recovery, idle-quit) takes the window only if the browser was
        # already on it. The rest of the reasoning is on force_window in
        # build_mpv_options -- only this half needs live player state.
        browser_wants_window = (
            self.mpvtk_active if reopen else not settings.start_minimized
        )
        mpv_options = build_mpv_options(
            osc_style,
            mpv_scripts(osc_style, trickplay_started),
            is_using_ext_mpv,
            browser_wants_window,
        )
        self._geometry_armed = mpv_options["geometry"]

        self._player = self._construct_mpv(
            mpv_options,
            input_default_bindings=True,
            input_vo_keyboard=True,
            input_media_keys=settings.media_keys,
            log_handler=mpv_log_handler,
            loglevel=mpv_loglevel_for(settings.mpv_log_level),
        )

        # **Before anything else touches this handle.** The shader pack is the
        # reason: `OSDMenu` / `menu.update_player` below construct a
        # VideoProfileManager, whose __init__ re-applies the remembered
        # profile -- and `default-setting-groups` writes `deband` and, via
        # `profile=gpu-hq`, the whole of `render_quality`. Snapshotted after
        # that, "the user's own value" would be the PACK's, so turning the
        # Debanding setting off would hand back `deband=yes, grain=0` over
        # the user's mpv.conf, for ever, with no profile loaded and no grain
        # shaders to justify the zero. `shader_pack_remember` defaults on, so
        # that is the ordinary path for anyone who has ever picked a profile.
        #
        # It also means a failure between here and the end of _init_mpv
        # cannot leave the PREVIOUS mpv's values recorded against this one.
        self._render_written = set()
        self._snapshot_render_pristine()

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

        # Group membership is not a property of the mpv handle either, and
        # gets there by a route the menu does not: stop() *halts* rather than
        # leaves, and idle_quit's gate (is_enabled) lets a halted member
        # through by design — so backing out to the library and letting mpv
        # quit is enough. A fresh manager here forgot we were in a group
        # while the server still held the seat: terminate() then never told
        # it we left, each cycle leaked the old manager's timesync
        # subscription and ping, and — since process_group_update is not
        # gated on membership — the group's next PlayQueue still reached the
        # new manager and drove playback, with no Ready ever sent, leaving
        # the whole group waiting on us. It talks to mpv only through this
        # PlayerManager, so unlike the menu it needs no re-pointing.
        if self.syncplay is None:
            self.syncplay = SyncPlayManager(self)

            if discord_presence:
                try:
                    register_join_event(self.syncplay.discord_join_group)
                except Exception:
                    log.error("Could not register Discord join callback.",
                              exc_info=True)

        if hasattr(self._player, "osc"):
            self.enable_osc(self.osc_enabled)
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
        # Separate from _audio_snapshot: the device survives a
        # return to "auto" mode, so it cannot share that reset.
        self._device_snapshot = None
        # A fresh mpv starts with stats.lua's overlay off — don't let a stale
        # flag make clear_stats() toggle it back on.
        self._stats_shown = False
        # The picture-option snapshot is NOT taken here -- it is taken the
        # moment the handle exists, above, because the shader pack writes to
        # this mpv before this line is reached. See _snapshot_render_pristine.
        # A fresh mpv may be a different build (the external binary can be
        # swapped under us), so the discovery is re-made rather than carried.
        self._no_deinterlace_auto = False
        # Likewise the colorspace hint: this mpv holds the user's own value,
        # so the flag must not claim it is already parked — the browser taking
        # the window back would then skip parking it (suspend_colorspace_hint).
        self._colorspace_hint_suspended = False
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

        self._bind_mpv_handlers()

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

    # -- mpv key bindings and event handlers -------------------------------
    #
    # These were nested inside _init_mpv until they outgrew it: thirty-one
    # handlers, defined inside a constructor, invisible to every AST-based
    # invariant test in the suite and impossible to call from a unit test.
    # None of them closed over anything but `self`, so they are methods that
    # had been written as closures.
    #
    # _bind_mpv_handlers is the whole binding table, in the order the
    # decorators ran. It re-runs per mpv instance (crash recovery,
    # idle-quit re-open), which is why no handler may hold state of its own.

    def _bind_key(self, key, func):
        """Bind one configurable key, ignoring the ones the user cleared.

        Was the `keypress` decorator factory; the emptiness check is the
        whole of it, and an unset keybind is a supported configuration.

        Empty string as well as None: the settings are Optional now, so a
        documented `null` really does arrive as None, but a config written
        under the old typing holds the string "None" and somebody clearing
        the field in a text editor leaves "". All three mean the same thing
        and none of them is a key.
        """
        if key and key != "None":
            self._player.on_key_press(key)(func)

    def _observe(self, prop, handler):
        """Register a property observer on either backend.

        Thin by design: the dispatch (and the bound-method hazard it exists
        to dodge) lives in ``mpv_events.observe``, so it can be checked
        against a real mpv without importing this module -- which would
        build a whole player as a side effect. Returns that function's
        token; nothing here unregisters, because mpv is torn down whole.
        """
        return observe_property(self._player, prop, handler)

    def _bind_mpv_handlers(self):
        """Attach every key binding and event handler to the current mpv."""
        p = self._player
        self._bind_key(settings.kb_stop, self._on_kb_stop)
        p.on_key_press("STOP")(self._on_stop_key)
        p.on_key_press("CLOSE_WIN")(self._on_close_win)
        self._bind_key(settings.kb_prev, self._on_prev_key)
        self._bind_key(settings.kb_next, self._on_next_key)
        # Two keydefs, one handler. Previously stacked decorators, which on
        # libmpv registered the *wrapper* of the inner binding under the outer
        # keydef and worked only because that wrapper's arguments all default.
        p.on_key_press("PREV")(self._on_media_prev)
        p.on_key_press("XF86_PREV")(self._on_media_prev)
        p.on_key_press("NEXT")(self._on_media_next)
        p.on_key_press("XF86_NEXT")(self._on_media_next)
        self._bind_key(settings.kb_watched, self._on_watched_key)
        self._bind_key(settings.kb_unwatched, self._on_unwatched_key)
        self._bind_key(settings.kb_menu, self._on_menu_key)
        self._bind_key(settings.kb_menu_esc, self._on_menu_esc)
        self._bind_key(settings.kb_menu_ok, self._on_menu_ok)
        # #16: `kb_menu_*` are MENU keys and are not bound here at all --
        # the OSD menu installs them itself for exactly as long as it is on
        # screen (claim_menu_keys), and the rest of the time the key is
        # mpv's. Seeking is mpv's too, unless the shim's own seek does
        # something mpv's cannot; then it is *claimed*, which follows a
        # remapped key where the fixed binding never did. See _seek_is_ours,
        # and docs/mpv-backends.md section 5 for why the split is here.
        self._bind_key(settings.kb_pause, self._on_pause_key)
        # #16: `f` is mpv's own key with mpv's own meaning, so it is no
        # longer bound here -- the fullscreen claim below takes whatever key
        # currently means `cycle fullscreen`, runs it, and records the
        # user's intent (which is the whole reason we needed the key at
        # all). A value the user explicitly set is still honoured: that is
        # their choice, not our interception.
        # Compared to the DEFAULT, for the reason input_conf._changed sets
        # out: __fields_set__ means "this key was in the file", and save()
        # writes all 186 of them, so it is true for every existing install
        # and this gate could never open. `f` stayed intercepted for
        # everyone -- including a user whose own input.conf puts fullscreen
        # on F11 and `f` on something else, which is the exact interception
        # the gate exists to prevent.
        if settings.kb_fullscreen != type(settings).kb_fullscreen:
            self._bind_key(settings.kb_fullscreen, self._on_fullscreen_key)
        self._bind_key(settings.kb_kill_shader, self._on_kill_shader_key)
        # Standing claim: recording "the user asked for fullscreen" is
        # always wanted, and interception is how it is known -- a change
        # that came through our binding is user-initiated by construction,
        # where an observer plus an ignore flag needs that flag set and
        # cleared around every self-initiated change and is wrong wherever
        # somebody forgets.
        from . import keysweep

        self._key_claims["fullscreen"] = {keysweep.FULLSCREEN}
        if self._seek_is_ours():
            self._key_claims["seek"] = {keysweep.SEEK}
        # Under the lock, which _refresh_key_section's contract requires and
        # this path did not hold: _bind_mpv_handlers is reachable from
        # set_browse_window / force_window, neither of which is
        # @synchronous, on the browser loop thread -- so a GroupJoined
        # landing on the websocket thread could mutate _key_claims while
        # this iterated it ("dictionary changed size during iteration",
        # out of _init_mpv, aborting the window rebuild), or simply lose
        # the race and install a section without the group's claim in it.
        with self._lock:
            self._refresh_key_section()
        p.on_key_press("i")(self._on_stats_oneshot)
        p.on_key_press("I")(self._on_stats_toggle)
        if settings.mouse_chapter_nav:
            # Playback only, and that is the renderer's doing rather than
            # ours: while the LIBRARY is up its mpvtk_thumb group is enabled
            # on top of this section, so back is still Back and forward is
            # still forward there. A summoned playback HUD leaves that group
            # disabled -- the thumb buttons belong to whatever the user has
            # under them over a film -- so these apply for the whole of
            # playback, bar and all. Bound at mpv creation like every other
            # key, so the setting needs a restart.
            p.on_key_press("MBTN_BACK")(self._on_chapter_prev_key)
            p.on_key_press("MBTN_FORWARD")(self._on_chapter_next_key)
        # Not a setting we push: on Wayland mpv writes `border` from the
        # decoration mode the compositor granted, so this is how the UI hears
        # that it has to draw its own title bar — and it can land after the
        # first frame, since the configure arrives asynchronously.
        self._observe("border", self._on_border_change)
        # The other two the UI's own title bar draws from: which glyph the
        # maximize button wears, and whether there is a title bar to draw at
        # all (there is not, fullscreen). Same handler -- the UI re-takes the
        # whole snapshot either way, so splitting them would only mean three
        # ways to get half of it.
        self._observe("window-maximized", self._on_border_change)
        self._observe("fullscreen", self._on_border_change)
        self._observe("eof-reached", self._on_eof_reached)
        self._observe("playback-abort", self._on_playback_abort)
        self._observe("seeking", self._on_seeking)
        self._observe("pause", self._on_pause_change)
        self._observe("paused-for-cache", self._on_cache_pause)
        self._observe("mute", self._on_volume_change)
        self._observe("volume", self._on_volume_change)
        p.event_callback("file-loaded")(self._on_file_loaded)
        self._observe("current-tracks/audio/codec", self._on_audio_codec_change)
        p.event_callback("end-file")(self._on_end_file)
        p.event_callback("shutdown")(self._on_shutdown_event)
        p.event_callback("client-message")(self._on_client_message)

    # -- key handlers ------------------------------------------------------

    def _on_kb_stop(self):
        # With the in-window browser, the window IS the library: q should
        # stop playback and drop back to browsing, not tear mpv down.
        # Closing the window (CLOSE_WIN) still quits.
        log.info("handle_stop triggered")
        if self.mpvtk_active:
            self.put_task(self.stop_to_browser)
        else:
            self.put_task(self.stop_and_close)

    def _on_stop_key(self):
        log.info("handle_stop triggered")
        self.put_task(self.stop_and_close)

    def _on_close_win(self):
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

    def _on_prev_key(self):
        self.put_task(self.play_prev)

    def _on_chapter_prev_key(self):
        self.put_task(self.chapter_seek, -1)

    def _on_chapter_next_key(self):
        self.put_task(self.chapter_seek, 1)

    def _on_next_key(self):
        self.put_task(self.play_next)

    def _on_media_prev(self):
        if settings.media_key_seek:
            seektime, _x = self.get_seek_times()
            self.seek(seektime)
        else:
            self.put_task(self.play_prev)

    def _on_media_next(self):
        if settings.media_key_seek:
            if self.is_in_intro and settings.skip_intro_on_seek:
                self.skip_intro()
            else:
                _x, seektime = self.get_seek_times()
                self.seek(seektime)
        else:
            self.put_task(self.play_next)

    def _on_watched_key(self):
        self.put_task(self.watched_skip)

    def _on_unwatched_key(self):
        self.put_task(self.unwatched_quit)

    def _on_menu_key(self):
        if self._library_has_input():
            # Browsing, the menu key is the context menu of whatever is
            # focused — the same thing the remote's hamburger does, and
            # the same key the renderer binds for it. The library's own
            # settings are a page, reached from the top bar.
            try:
                self._player.command("keypress", "MENU")
            except Exception:
                log.debug("context menu keypress failed", exc_info=True)
            return
        self.toggle_settings_menu()

    def toggle_settings_menu(self):
        """The player's own settings menu, toggled.

        Two callers with one meaning: the kb_menu key, and a remote whose
        cog or hamburger found no page to open (menu_action). Which surface
        that is depends on the OSC — the HUD's gear menu under mpvtk, the
        OSD menu otherwise — and deciding it twice is how they would drift.
        """
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

    def _on_menu_esc(self):
        if self.menu.is_menu_shown:
            self.menu.menu_action("back")
        elif self._nav_back():
            pass    # the in-window UI consumed it (dialog / go back)
        else:
            self._player.command("set", "fullscreen", "no")
            self.fullscreen_disable = True

    def _on_menu_ok(self):
        """ENTER: confirm the OSD menu, and nothing else.

        It must not *open* it: ``menu_action("ok")`` on a hidden menu is
        ``show_menu()``, and under mpvtk the OSD menu draws as mpv OSD text,
        so it lands under the overlay bitmaps and takes the arrow keys with
        it -- the same thing ``toggle_settings_menu`` refuses above.

        Swallowed rather than left to mpv, which binds ENTER to
        ``playlist-next``: our mpv playlist holds one file, so what that does
        depends on ``keep-open`` and is not a behaviour to inherit by
        accident. Part of #16, except that here we mean nothing by the key at
        all.
        """
        if self.menu.is_menu_shown:
            self.menu.menu_action("ok")

    # _on_menu_left / _right / _up / _down are gone with #16. Nothing bound
    # them once `kb_menu_*` became the MENU's keys: the menu's own section
    # sends `jms-menu <action>` straight to `menu_action`, and seeking is
    # mpv's (or a claim). Their skip-intro and web-seek branches live in
    # `_on_seeking` and `_on_claimed_key` respectively.

    def _on_pause_key(self):
        if self.menu.is_menu_shown:
            self.menu.menu_action("ok")
        else:
            self.toggle_pause()

    def _on_fullscreen_key(self):
        self.toggle_fullscreen()

    # Kill shader packs (useful for breakage)
    def _on_kill_shader_key(self):
        # Suppressed until the user picks again, not merely unloaded. Since
        # profiles resolve per item (series -> library -> default), an
        # unload alone lasted until the next episode, which then put the
        # override's profile straight back -- on the one key whose entire
        # purpose is recovering from a profile that breaks playback.
        if self.menu is not None and self.menu.profile_manager is not None:
            self.menu.profile_manager.suppressed = True
        if settings.shader_pack_remember:
            settings.shader_pack_profile = None
            settings.save()
        if self.menu.profile_manager is not None:
            self.menu.profile_manager.unload_profile()

    # mpv's stats.lua binds `i`/`I` to its "Playback Data" overlay. Take
    # them over so the in-window UI stays in charge of that overlay: under
    # the in-window OSC the overlay is tracked (so it can be cleared when
    # the library returns) and swallowed while browsing/idle (it is ASS
    # OSD that would paint behind the library). The classic/lua OSCs and
    # CLI keep mpv's stock behaviour.
    def _stats_key(self, oneshot):
        if (self.mpvtk_active and self._video is not None
                and not self._current_is_audio()):
            # A video is on screen under the in-window HUD: track the
            # overlay so clear_stats() can hide it when the library
            # returns. Audio keeps the browser up (no picture to
            # annotate), so it falls through to the swallow below.
            self.put_task(self.toggle_stats)
        elif not self.mpvtk_active:
            self._player.command(
                "script-binding",
                "stats/display-stats" if oneshot
                else "stats/display-stats-toggle")
        # else: browsing / audio / idle under the in-window UI —
        # swallow it so the overlay isn't painted over the library.

    # Two bindings, because a key handler is called with no arguments and
    # the oneshot flag is the only thing that differs between them.
    def _on_stats_oneshot(self):
        self._stats_key(True)

    def _on_stats_toggle(self):
        self._stats_key(False)

    # -- property observers and event callbacks ----------------------------

    # Fires between episodes.
    def _on_eof_reached(self, _name, reached_end: bool):
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
    def _on_playback_abort(self, _name, value: bool):
        if self._video and value and not self._video.parent.has_next:
            self._queue_finished()

    def _on_seeking(self, _name, value: bool):
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

    def _on_pause_change(self, _name, value: bool):
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

    def _on_cache_pause(self, _name, value: bool):
        """mpv stalled waiting for the demuxer cache, or recovered.

        SyncPlay has a Buffer request for exactly this: the group pauses for a
        member that has stalled, and resumes when it reports Ready. We only
        ever raised it from the `seeking` property, so a cache underrun --
        the common case, and the one the feature exists for -- was never
        reported. This client simply fell behind and was then yanked back by
        SkipToSync.

        on_buffer debounces by min_buffer_thresh_ms, so a brief stall costs
        nothing. Loads are excluded by do_not_handle_pause, which is set for
        the whole of a playback start; without that every file would announce
        itself as buffering while it filled its cache.
        """
        if self.do_not_handle_pause or not self.syncplay.is_enabled():
            return
        if value:
            self.syncplay.on_buffer()
        else:
            self.syncplay.on_buffer_done()

    def _on_volume_change(self, _name, _value):
        """Volume or mute moved -- tell the UI now rather than on the tick.

        The HUD's mute icon and volume bar read the playstate snapshot, and
        nothing pushed one when either changed, so the button showed the old
        icon for up to a second after the audio stopped (#618).

        push_playstate() directly, not timeline_handle(): the timeline thread
        also POSTs progress to the server, and a volume nudge is not worth a
        request.

        Observing mpv rather than patching state at our own button is what
        makes mpv's OWN bindings -- `m`, the wheel, a script -- move it too.
        """
        self.push_playstate()

    def _on_file_loaded(self, _event):
        # Mirrors _on_end_file's generation guard: a file-loaded from
        # the OUTGOING file (keep_open holds it until the replacement
        # lands) must not be taken as the incoming one having loaded.
        if self._loading:
            self._load_completed.set()
        # Whether the AC3 encoder belongs in the chain depends on this
        # file's audio codec. Deferred onto the action thread: issuing mpv
        # commands from inside an event handler is what put_task exists to
        # avoid.
        self.put_task(self.apply_audio_filters)

    def _on_audio_codec_change(self, _name, _value):
        # Track switches change the answer as much as file changes do:
        # moving from an AC3 track (passed through) to a 5.1 AAC one needs
        # the encoder attached, or the surround is silently lost. Observed
        # rather than hooked into set_streams so that mpv's own track
        # cycling is covered too.
        self.put_task(self.apply_audio_filters)

    def _on_end_file(self, event):
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

    def _on_shutdown_event(self, event):
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

    #: What makes the shim's seek different from mpv's, and so what is worth
    #: claiming a key for. Only ``use_web_seek`` is left: the six seek
    #: distance settings moved into the user's input.conf in #16, and while
    #: they were here they were actively misleading (a changed distance made
    #: this return True, and the claim then seeked by the amount in *mpv's*
    #: binding). ``skip_intro_on_seek`` is deliberately absent -- `_on_seeking`
    #: applies it to any forward seek, mpv's own included, so a claim would
    #: double-handle. See docs/mpv-backends.md section 5.
    _MPV_EQUIVALENT_SEEK = {"use_web_seek": False}

    def _seek_is_ours(self):
        """Whether the shim's seek does something mpv's cannot.

        Four things can: a changed distance, an exact-seek flag,
        jellyfin-web's variable seek, and skip-intro-on-seek. None of them
        and seeking is mpv's outright -- the defaults here are `seek 60` /
        `seek -60` / `seek 5` / `seek -5`, character for character what mpv
        binds, so the old arrow bindings existed to reimplement mpv's
        arrows exactly.

        Any of them and the keys are *claimed* rather than bound: the claim
        follows whatever currently means seek, so somebody who moved seeking
        onto other keys gets their features there too, which four fixed
        bindings never gave them.
        """
        return any(getattr(settings, key, default) != default
                   for key, default in self._MPV_EQUIVALENT_SEEK.items())

    def set_osc_style(self, style: str):
        """Override the resolved OSC style for this mpv instance.

        One caller: the no-lua fallback. Every OSC the shim can offer is a
        lua script, so when lua is unavailable there is no style to resolve
        *to* -- and leaving the resolved value at "mpvtk" is what kept the
        OSD menu unreachable, since ``toggle_settings_menu`` refuses it
        under that style alone.
        """
        self._osc_style_override = style
        self._osc_style_resolved = style
        self._osc_script_loaded = False

    def lua_works(self, timeout: float = 2.0):
        """Whether this mpv can run a lua script at all. Cached.

        **Everything the shim draws needs this** -- the browser and the HUD
        are renderer.lua, the stock OSC is lua, mouse.lua is lua.

        A probe, not a capability string: ``mpv-configuration`` does not
        mention lua on every build, and ``load-script`` on a script that
        cannot run **raises nothing on either backend**. Only a script
        reporting back is proof, which also catches lua that loads and then
        errors. Costs ~10 ms when lua works, and the full timeout once when it
        does not. See docs/mpv-backends.md section 2.
        """
        if self._lua_works is not None:
            return self._lua_works
        answered = threading.Event()
        self._lua_probe = answered
        try:
            self._player.command("load-script",
                                 get_resource("lua_probe.lua"))
            answered.wait(timeout)
        except Exception:
            log.debug("lua probe could not be loaded", exc_info=True)
        finally:
            self._lua_probe = None
        self._lua_works = answered.is_set()
        if not self._lua_works:
            log.warning("This MPV cannot run lua scripts. The library "
                        "browser, the playback HUD and the on-screen "
                        "controls all need it; falling back to the "
                        "command line and the OSD menu.")
        return self._lua_works

    # ------------------------------------------------------ the OSD menu

    #: The flags every section of ours is enabled with -- the same string
    #: mpv's own defaults.lua gives a script's bindings.
    #:
    #: Without ``allow-hide-cursor`` an enabled section covering the pointer
    #: tells mpv a script's UI is under the mouse, which re-arms the autohide
    #: timer on every poll, so **the cursor never hides again** -- and ours
    #: cover the whole screen by default while holding only keyboard keys.
    #: Without ``allow-vo-dragging`` mpv refuses to move the window from a
    #: drag on the video. renderer.lua withholds the first from its own mouse
    #: sections on purpose; these are enabled for the life of the process.
    #: See docs/mpv-backends.md section 5.
    SECTION_FLAGS = "allow-hide-cursor+allow-vo-dragging"

    #: Section and message for the OSD menu's own arrow keys.
    MENU_SECTION = "jms_menu"
    MENU_MESSAGE = "jms-menu"

    #: ``key setting -> menu action``. ENTER and ESC are deliberately absent:
    #: they keep their Python bindings, which are already guarded on
    #: ``is_menu_shown`` and have duties outside the menu.
    _MENU_KEYS = (("kb_menu_left", "left"), ("kb_menu_right", "right"),
                  ("kb_menu_up", "up"), ("kb_menu_down", "down"))

    def claim_menu_keys(self, wanted):
        """Give the arrows to the OSD menu for as long as it is on screen.

        Installed on show and torn down on hide, which is what lets them
        belong to mpv the rest of the time with no conditional behaviour
        for the user to notice -- the same lifecycle the renderer runs for
        its own mouse sections.

        Unconditional since the arrows stopped being bound in Python: this
        is the only thing that gives the menu its navigation, so a gate here
        is a menu that cannot be driven.
        """
        try:
            if not wanted:
                self._player.command("disable-section", self.MENU_SECTION)
                return
            lines = "\n".join(
                "%s script-message %s %s" % (getattr(settings, key),
                                             self.MENU_MESSAGE, act)
                for key, act in self._MENU_KEYS
                if getattr(settings, key, None)
            )
            self._player.command("define-section", self.MENU_SECTION,
                                 lines, "force")
            self._player.command("enable-section", self.MENU_SECTION,
                                 self.SECTION_FLAGS)
        except Exception:
            log.debug("could not update the menu key section", exc_info=True)

    # ------------------------------------------------------- key claims
    #
    # #16. Rather than binding `space`, `f` and the arrows for ever, ask mpv
    # which keys currently MEAN pause/seek/fullscreen (keysweep) and take
    # only those, only while something needs them. A claim re-issues the
    # user's own intent through the shim's SyncPlay-aware operations, so the
    # key keeps meaning what their config says it means.
    #
    # An input SECTION rather than per-key bindings, because that is the one
    # mechanism both backends have: libmpv can unregister a key binding and
    # python_mpv_jsonipc cannot (it has bind_key_press and no unbind at
    # all), while define-section/enable-section/disable-section are ordinary
    # commands on either.

    #: The section's name, and the script-message verb its lines send.
    KEY_SECTION = "jms_keys"
    KEY_MESSAGE = "jms-key"

    def claim_keys(self, owner, semantics=None):
        """Take, or give back, every key that currently means one of
        ``semantics``. ``None`` releases ``owner``'s claim.

        Owners are independent and the section is the union, so SyncPlay
        joining a group does not disturb the standing fullscreen claim.
        """
        with self._lock:
            if semantics:
                self._key_claims[owner] = set(semantics)
            else:
                self._key_claims.pop(owner, None)
            self._refresh_key_section()

    def _swept_keys(self):
        """The sweep, done ONCE and cached.

        Cached because it must not see our own section: the lines we install
        are non-weak, so a re-sweep would find `script-message jms-key ...`
        winning every claimed key, classify it as nothing, and quietly drop
        the claim on the next refresh. Bindings do not change at runtime
        anyway -- mpv reads input.conf at startup.
        """
        if self._swept is None:
            from . import keysweep

            try:
                bindings = self._player.input_bindings
            except Exception:
                log.debug("could not read input-bindings", exc_info=True)
                bindings = []
            self._swept = keysweep.sweep(
                bindings, {keysweep.PAUSE, keysweep.SEEK,
                           keysweep.FULLSCREEN})
            log.debug("key sweep: %s", self._swept)
        return self._swept

    def _swept_pointers(self):
        """Pointer keys meaning seek, cached alongside the sweep and for the
        same reason -- see :meth:`_swept_keys`."""
        if self._swept_ptr is None:
            from . import keysweep

            try:
                bindings = self._player.input_bindings
            except Exception:
                bindings = []
            self._swept_ptr = keysweep.pointer_keys(bindings,
                                                    {keysweep.SEEK})
        return self._swept_ptr

    def _refresh_key_section(self):
        """Rebuild and re-enable the section for the current claims. Callers
        hold ``_lock``."""
        from . import keysweep

        wanted = set()
        for owned in self._key_claims.values():
            wanted |= owned
        claims = [c for c in self._swept_keys() if c[1] in wanted]
        self._key_actions = {key: (semantic, arg)
                             for key, semantic, arg in claims}
        # Pointer keys are never *claimed* -- that is the renderer's ground
        # -- but a seek nobody reports is a desync a SyncPlay group then
        # corrects, and mpv binds WHEEL_LEFT/RIGHT to one. Suppressed for
        # the duration rather than routed: a wheel gesture delivers dozens
        # of notches, all to reach an operation the group would refuse.
        suppress = ([] if keysweep.SEEK not in wanted
                    else self._swept_pointers())
        try:
            if not claims and not suppress:
                self._player.command("disable-section", self.KEY_SECTION)
                return
            self._player.command(
                "define-section", self.KEY_SECTION,
                keysweep.section_lines(claims, self.KEY_MESSAGE, suppress),
                "force")
            self._player.command("enable-section", self.KEY_SECTION,
                                 self.SECTION_FLAGS)
        except Exception:
            log.debug("could not update the key section", exc_info=True)

    def _on_claimed_key(self, semantic, key):
        """A claimed key was pressed: carry out what the user had bound,
        through the operation that knows about SyncPlay and about
        remembering the choice."""
        found = self._key_actions.get(key)
        if found is None:
            return
        _semantic, arg = found
        if semantic == "pause":
            if arg is None:
                self.toggle_pause()
            else:
                # `set pause yes/no` -- PAUSEONLY and PLAYONLY. Answering
                # these with a toggle would pause a playing file from the
                # key whose entire job is not to.
                self.set_paused(bool(arg))
        elif semantic == "seek":
            amount, exact = arg
            # jellyfin-web's variable seek, applied to whatever key the
            # user actually seeks with -- which four fixed arrow bindings
            # never managed. Routed by SIGN, because that is all a binding
            # can tell us: `seek -5` and `seek -60` are both "backwards",
            # and which of them was the "left" key is neither recoverable
            # nor needed, since web seek replaces the distance anyway.
            #
            # Nothing here for skip-intro: `_on_seeking` catches the seek
            # this is about to make, exactly as it catches mpv's own.
            if settings.use_web_seek:
                back, forward = self.get_seek_times()
                amount = forward if amount > 0 else back
            self.seek(amount, exact=exact)
        elif semantic == "fullscreen":
            want = (not self._player.fs) if arg is None else bool(arg)
            self.set_fullscreen(want, persist=True)

    def _on_client_message(self, event):
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
            elif args[0] == "shim-menu-back":
                self.menu.menu_action("back")
            elif args[0] == self.KEY_MESSAGE and len(args) >= 3:
                self._on_claimed_key(args[1], args[2])
            elif args[0] == self.MENU_MESSAGE and len(args) >= 2:
                self.menu.menu_action(args[1])
            elif args[0] == "shim-trickplay-need" and len(args) >= 2:
                # A preview was asked for at a position the loaded window
                # does not cover. Only the OSC side knows where the pointer
                # is, so the request comes from there. This is mpv's event
                # thread -- request_at records and wakes the worker, and
                # must keep doing only that.
                trickplay = self.trickplay
                if trickplay is not None:
                    trickplay.request_at(float(args[1]))
            elif args[0] == "jms-lua":
                if self._lua_probe is not None:
                    self._lua_probe.set()
        except Exception:
            log.warning("Error when processing client-message.", exc_info=True)

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

        ``_lock`` is held for the whole of a playback start -- the mpv load
        plus the duration wait, bounded only by playback_timeout (30s) and
        routinely the full timeout when a stream fails to open. UI actions run
        inline on the browser's loop thread, so calling a @synchronous method
        straight through froze the window for that whole stretch.

        Fast path is unchanged: a free lock means the action runs inline and
        synchronously. Only a contended lock defers onto the action thread.
        ``func`` takes the PlayerManager.

        NB the deferred path holds **no** lock, which is why targets that
        mutate the player carry their own ``@synchronous``.
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
    def chapter_seek(self, direction):
        """Jump a chapter back (-1) or forward (+1).

        Not mpv's own ``add chapter``: that bypasses SyncPlay, so one member
        of a group jumping a chapter would simply desync. Going through
        seek() puts it through the same request the seek bar makes.

        Exempt from seek-to-skip-intro for the same reason the HUD's own
        seeks are: someone jumping to the chapter the intro is in asked for
        that chapter, not for the end of the intro.
        """
        try:
            chapters = self._player.chapter_list or []
            pos = self._player.playback_time
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        if pos is None:
            return
        target = chapter_target(
            [{"time": float(ch.get("time") or 0.0)} for ch in chapters],
            float(pos), direction)
        if target is None:
            return
        self._last_ui_seek_time = time.time()
        self.seek(target, absolute=True)

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
            position = self._player.playback_time or 0
            if idle is None:
                live = position > 0
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
        # Where playback actually is, not zero: the worker loads a window
        # around it, and on a resumed item the first place anyone scrubs is
        # where they already are. Whole-video mode ignores it.
        self.trickplay.fetch_thumbnails(position)

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
                conf.any_segment_wanted()
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
                    action = conf.segment_action(intro.type)
                    # In a SyncPlay group "always" degrades to "ask" rather
                    # than switching the feature off. Skipping is a seek, and
                    # a seek is the *group's*: done automatically it yanks
                    # everyone, and with several members set to always they
                    # race to do it. Offering the button keeps the feature
                    # and makes the seek one deliberate, attributable act --
                    # which is what a group seek should be. The whole block
                    # used to be gated off, so a group got no button either.
                    in_group = self.syncplay.is_enabled()
                    should_skip = (not intro.has_triggered
                                   and action == "always"
                                   and not in_group)
                    should_prompt = (action == "ask"
                                     or (action == "always" and in_group))

                    if should_skip and ready_to_skip:
                        intro.has_triggered = True
                        self.skip_intro()
                        self._player.show_text(
                            segment_labels(intro.type)[1], 3000, 1)
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
                            segment_labels(intro.type)[2], 3000, 1)
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
        pause_stills: bool = True,
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
            # BEFORE the url is built: whether the header took decides
            # whether the url has to carry the token itself.
            video.auth_via_header = self._apply_auth_headers(video)
            # Also before the url: the aid handed to PlaybackInfo is what the
            # server bakes into a transcode, so a track settled afterwards
            # cannot be heard. The rule first, then the remembered choice over
            # it -- the precedence the old ordering gave for free, when the
            # rule ran in map_streams and memory overwrote it in _play_media.
            video.resolve_tracks_for_negotiation()
            if is_initial_play:
                self._track_memory = None  # new queue; start fresh
            elif apply_memory and self._track_memory is not None:
                self._apply_remembered_tracks(video)
            url = video.get_playback_url()
            if not url:
                log.error("PlayerManager::play no URL found")
                # Even so: the header we just installed is a GLOBAL option
                # that outlives this attempt, so it must not sit there
                # pointing at whatever plays next.
                self._revoke_auth_header(video)
                return
            # ...and AFTER, because only now is the stream's host known.
            # `_apply_auth_headers` can only ask about subtitles -- the media
            # host does not exist until the negotiation above has run. This
            # order is deliberate in both directions and neither half can move:
            # the install has to precede the url so the url knows whether to
            # carry a token, and the origin check has to follow it.
            if video.auth_via_header and not same_origin(
                    url, video.client.config.data.get("auth.server")):
                # A direct path to somebody else's host: an http `.strm`
                # source, or a path substitution. Revoking rather than never
                # installing is what keeps the failure modes above intact --
                # that branch of `_get_url_from_source` returns the url
                # unchanged and never embeds a token, so there is nothing for
                # the url to lose here.
                log.info("Not sending the auth header to mpv: this item "
                         "streams from another host.")
                self._revoke_auth_header(video)
            self._play_media(video, url, offset, no_initial_timeline,
                             is_initial_play, apply_memory, pause_stills)
        finally:
            self._start_in_progress = False

    def _apply_auth_headers(self, video):
        """Hand mpv this server's Authorization header. True if it took.

        Everything mpv fetches for this file goes through it -- stream and
        subtitle sidecar alike -- so no URL needs a token in its query string.
        ``Authorization: MediaBrowser Token="..."`` is the one scheme not gated
        behind ``EnableLegacyAuthorization``; the apiclient already builds it.

        Returns False rather than raising, and the caller falls back to the
        url; the cost of being wrong is that nothing plays at all.

        **The clear at the top is load-bearing.** ``http-header-fields`` is a
        global, persistent option and mpv is not re-created between queue
        items, so without it a refusal to send the header defeats itself and
        leaks the previous item's token to a third-party host. Clearing here
        rather than on each ``return False`` covers every exit path past this
        line, including the ones nobody has written yet.
        See docs/mpv-backends.md section 6.
        """
        if not self._mpv_alive:
            # mpv is DOWN — idle-quit, a crash, a window the user closed —
            # and _play_media re-creates it moments from now, in
            # _ensure_mpv, which runs after this. Touching the dead handle
            # from here is not an exception to catch: libmpv's property
            # write on a destroyed handle takes the process with it, and
            # the try/except below cannot see that coming. The re-opened
            # mpv holds no header of ours in any case, so the honest
            # answer is this method's documented fallback — let the url
            # carry the token, and the next start install the header.
            return False
        try:
            self._player.http_header_fields = []
        except Exception:
            log.debug("could not clear http-header-fields", exc_info=True)
        client = getattr(video, "client", None)
        if client is None:
            return False
        try:
            header = client.http._get_authenication_header()
        except Exception:
            log.debug("could not build an auth header", exc_info=True)
            return False
        if not header or "Token=" not in header:
            # No token yet (an unauthenticated probe): nothing to send, and
            # claiming success would strip a url that needs one.
            return False
        try:
            foreign = video.foreign_subtitle_hosts()
        except Exception:
            log.debug("could not check for foreign subtitle hosts",
                      exc_info=True)
            foreign = {"unknown"}
        if foreign:
            # http-header-fields is GLOBAL: mpv would send this token to
            # whoever hosts that subtitle. There is no per-URL header option,
            # so the only safe answer is to not set it at all and let the
            # stream URL carry its own token, which is where it was before.
            log.info("Not sending the auth header to mpv: this item has a "
                     "subtitle on %s, and the option is not per-URL.",
                     ", ".join(sorted(str(h) for h in foreign)))
            return False
        try:
            self._player.http_header_fields = ["Authorization: " + header]
        except Exception:
            log.warning("mpv would not take http-header-fields; falling back "
                        "to a token in the URL", exc_info=True)
            return False
        return True

    def _revoke_auth_header(self, video=None):
        """Take back a header installed before the stream's host was known.

        ``http-header-fields`` is global and outlives the item it was set for
        (docs/mpv-backends.md section 6), so "do not send it to this host" has
        to mean clearing it, not merely declining to set it again.
        """
        if video is not None:
            video.auth_via_header = False
        if not self._mpv_alive:
            return
        try:
            self._player.http_header_fields = []
        except Exception:
            log.debug("could not clear http-header-fields", exc_info=True)

    def _forced_hwdec(self):
        """Whether a shader profile has named the decoder it requires.

        A profile naming a *specific* decoder (``d3d11va``, ``vaapi``, …)
        is stating a hardware requirement of the thing the user just chose
        -- the shipped ``rtx-vsr`` needs d3d11va because its d3d11vpp
        filter operates on d3d11 surfaces. That is different in kind from
        the blanket ``auto-copy`` every profile used to carry, which was an
        opinion about the machine. The specific one is applied by the
        profile itself; this just stops the per-item write from undoing it
        on the next file.
        """
        try:
            profiles = self.menu.profile_manager if self.menu else None
            return bool(profiles is not None
                        and getattr(profiles, "forced_hwdec", None))
        except Exception:
            log.debug("could not read the shader profile", exc_info=True)
            return False

    def _needs_copy_hwdec(self):
        """Whether anything downstream needs frames in system RAM.

        Direct hardware decoding hands mpv frames on the GPU, which a video
        filter cannot read -- so where there is a filter, hwdec has to be the
        copy-back kind or it silently does not apply. Three sources:

        * the active shader profile said so (``wants_copy_hwdec``);
        * SVP is enabled, i.e. a VapourSynth filter in the user's mpv.conf;
        * mpv reports a filter chain -- the general case, asked separately
          because it is the only one that sees a filter we know nothing about.

        Never raises: unanswerable means "no filter", and the cost of being
        wrong is a filter that does not apply, not a player that fails.
        """
        try:
            profiles = self.menu.profile_manager if self.menu else None
            if profiles is not None and getattr(profiles, "wants_copy_hwdec",
                                                False):
                return True
        except Exception:
            log.debug("could not read the shader profile", exc_info=True)
        if settings.svp_enable:
            return True
        try:
            return bool(self._player.vf)
        except Exception:
            log.debug("could not read the filter chain", exc_info=True)
        return False

    @synchronous("_lock")
    def _play_media(
        self,
        video: "Video_type",
        url: str,
        offset: int = 0,
        no_initial_timeline: bool = False,
        is_initial_play: bool = False,
        apply_memory: bool = True,
        pause_stills: bool = True,
    ):
        self._ensure_mpv()

        self.pause_ignore = True
        self.do_not_handle_pause = True
        self.url = url
        self._showing_browse_bg = False   # real media replaces the backdrop
        # A start supersedes any browse background a previous one deferred.
        self._browse_bg_deferred = False
        # Real media wants the user's colorspace hint back, HDR passthrough
        # included; the browser parks it while it holds an idle window.
        self.resume_colorspace_hint()
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
        # A shader pack is for moving pictures. It is applied once and
        # left on the mpv instance, so without this an anime-upscaling
        # chain runs over a photograph -- and over a comic page, which is
        # 1600x2400 or larger, where it is expensive as well as wrong. The
        # name is kept while suspended, so the menu still shows the profile
        # the user chose and nothing rewrites the remembered setting.
        #
        # apply_for_item, not resume_after_still: the profile is resolved
        # per item now (series -> library -> the global setting), so the
        # answer for this file is not necessarily the answer for the last
        # one, and a still having suspended it is only the commonest reason
        # for that rather than the only one. It takes the client explicitly
        # because self._video is not this video yet.
        try:
            profiles = self.menu.profile_manager if self.menu else None
            if profiles is not None:
                if getattr(video, "is_photo", False):
                    profiles.suspend_for_still()
                else:
                    profiles.apply_for_item(v_item,
                                            getattr(video, "client", None))
        except Exception:
            log.debug("could not adjust the shader profile for this item",
                      exc_info=True)
        # Hardware decoding, BEFORE play() for the same reason as the two
        # above: hwdec is read when the decoder is initialised, and the
        # failures this setting is cautious about (a driver that resets the
        # GPU, a vp9 path that hangs before the window opens) happen there.
        # Setting it afterwards would apply to the file after this one.
        #
        # Re-applied per item rather than only at construction, which is
        # what lets "over-1080p" be a policy at all -- and, for the static
        # modes, what makes a settings change take effect on the next item
        # instead of the next launch.
        try:
            from .mpv_options import hwdec_for

            # None means somebody more specific has already spoken -- the
            # user's own mpv.conf, or a shader profile naming the decoder
            # its filter requires. Both outrank the setting, and both are
            # already applied, so the right move is not to write at all.
            from .args import get_args

            # --disable-hwdec outranks EVERYTHING, including a profile that
            # named its decoder. It is the recovery path for hardware
            # decoding stopping the window opening at all, so a shader
            # profile silently defeating it would leave the user with no way
            # back in -- which is the one thing this flag exists to prevent.
            if getattr(get_args(), "disable_hwdec", False):
                self._player.hwdec = "no"
            else:
                forced = self._forced_hwdec()
                want = None if forced else hwdec_for(
                    _source_height(video), self._needs_copy_hwdec())
                if want is not None:
                    self._player.hwdec = want
        except Exception:
            # Never let a decode *preference* stop playback: mpv keeps
            # whatever it had, which is at worst the previous item's.
            log.debug("could not apply the hwdec setting", exc_info=True)
        # Deinterlacing and the preset-driven settings (interpolation,
        # debanding, tone mapping, render quality, buffering), per item and
        # for the same reason hwdec is: all of them are plain settings, and
        # applying them only at construction would mean a change took effect
        # on the next launch rather than the next thing played. They are
        # also cheap property writes rather than construction options, so
        # there is nothing to gain by doing it earlier.
        #
        # The buffering preset is the one whose granularity is not a
        # choice: the demuxer reads those options when it starts, so
        # per-item is the earliest they can land anyway.
        #
        # Separately guarded from hwdec above, not folded into its try:
        # they are unrelated preferences, and an mpv that rejects one has
        # no bearing on the others. Sharing a block means the first failure
        # silently drops the rest.
        try:
            from .mpv_options import deinterlace_value

            self._apply_deinterlace(
                deinterlace_value(self._deinterlace_override))
        except Exception:
            log.debug("could not apply the deinterlace setting",
                      exc_info=True)
        self._apply_render_presets()
        # How long mpv holds a still. BEFORE play(), not after the load
        # succeeds: this is what mpv reports as the file's `duration`, so
        # the duration wait below and the HUD's scrub bar both depend on it
        # already being right.
        #
        # It has to be set at all because the in-window browser parks it at
        # "inf" while it owns the window (set_browse_window) and browse_yield
        # deliberately does not undo that -- so a photo opened from the
        # library inherited "inf", displayed forever, and never reached
        # end-of-file. That is the whole of "photo auto-advance is broken":
        # the queue was waiting on an EOF mpv had been told never to send.
        if getattr(video, "is_photo", False):
            try:
                self._player.image_display_duration = max(
                    1, int(settings.photo_display_secs))
            except (_mpv_errors, ValueError, TypeError):
                log.debug("could not set the photo display duration",
                          exc_info=True)
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
        # Carried down to the set_paused() below, which is unconditional and
        # would otherwise undo this a few dozen lines later.
        hold_still = bool(
            getattr(video, "is_photo", False) and pause_stills
            and is_initial_play)
        if hold_still:
            # A photo is a video that happens to be still: mpv holds it for
            # --image-display-duration and then advances, which is a
            # slideshow nobody asked for when they opened one picture.
            #
            # Both guards earn their place, and neither did on its own:
            # `is_initial_play` -- pausing is about the picture you opened,
            # not every picture after it, or the slideshow advances one frame
            # and stops. `pause_stills` -- what the *request* asked for; Play
            # All on an album is "run the slideshow", and pausing on frame one
            # would be a queue that never starts.
            try:
                self._player.pause = True
            except _mpv_errors:
                log.debug("could not pause on a photo", exc_info=True)
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
        # The remembered track is applied in play(), before the url: doing it
        # here was after the negotiation AND after the load, and
        # configure_streams skips audio on a transcode because the audio is
        # already encoded into the stream. What is left here is the capture.
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
            # `hold_still`, not False: a photo opened on its own was paused
            # above and this call undid it, so the picture the user asked to
            # look at started a slideshow instead. The unpause predates the
            # photo branch by five years, which is why it reads as unrelated.
            # Still routed through set_paused rather than left to the earlier
            # assignment, because this is also what pushes the playstate.
            self.set_paused(hold_still, False)

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

        # Not under the in-window OSC: the warning is mpv OSD text, so it
        # draws *under* the mpvtk overlay bitmaps, and the key it names goes
        # to the HUD's gear menu rather than the OSD menu it was written for.
        # The same information is on the gear menu's quality entry, which
        # marks the current stream "Transcode".
        if (
            not self._video.parent.is_local
            and self._video.is_transcode
            and not self.warned_about_transcode
            and settings.transcode_warning
            and getattr(self, "_osc_style_resolved", None) != "mpvtk"
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

    def _release_syncplay(self):
        """Stopping playback while in a SyncPlay group: halt, or leave.

        Halting is what jellyfin-web does -- membership is not a property of
        playback, and dropping the group every time somebody went back to the
        library made SyncPlay unusable for anything but one film start to
        finish. But halting only works if the group is still **reachable**
        afterwards, and on two surfaces it is not: with no GUI at all, and
        when playback was cast to a shim whose browser was never opened. The
        alternative there is a group the user is in, is not watching, and has
        no way to get out of.

        Already halted is nothing to release: a halted member may go on to
        play something of their own, and that ending is not the group's
        business.
        """
        if not self.syncplay.is_enabled():
            return
        ask = self.syncplay_menu_reachable
        reachable = False
        if ask is not None:
            try:
                reachable = bool(ask())
            except Exception:
                log.debug("syncplay_menu_reachable failed", exc_info=True)
        if reachable:
            self.syncplay.halt_group_playback()
        else:
            log.info("Leaving the SyncPlay group: no menu to leave it from later.")
            self.syncplay.disable_sync_play(False)

    @synchronous("_lock")
    def stop(self, leave_group: bool = True):
        """Stop playback.

        ``leave_group`` is False only for a SyncPlay group Stop: the server
        moves the group to Idle and keeps every session in it, so tearing our
        own membership down would leave us out of a group the server still
        thinks we are in.
        """
        if leave_group and self.syncplay.is_enabled():
            self._release_syncplay()

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
        # After the stop, never before it: see clear_media_title.
        self.clear_media_title()
        # As early as it can be true, and before any of the teardown below:
        # this is what sends the browser back to the library (and hides the
        # music bar), and every line after it is bookkeeping the user has no
        # reason to wait through. It used to come last, so a slow stop report
        # or a stop_cmd hook was time spent staring at a dead window.
        self.push_playstate(stopped=True)
        self.release_stream(local_video)
        if local_video.client is None and hasattr(local_video,
                                                  "record_offline_progress"):
            local_video.record_offline_progress(options.get("PositionTicks"))
        self.send_timeline_stopped(options=options, client=local_video.client)
        self.exec_stop_cmd()

        if self.trickplay:
            self.trickplay.clear()





    def stop_for_window_close(self):
        """Stop because the window is going away.

        Always LEAVES a SyncPlay group rather than halting it. Halting is for
        a stop you can come back from -- back to the library, where the
        SyncPlay menu is. A closing window has no library behind it, so a
        halted membership is one nobody can see, leave or resume while the
        group goes on waiting for a member who is not there.

        Explicit rather than left to `syncplay_menu_reachable`: that hook does
        answer "no" here, but only because the browser happens to call
        minimize() first, with nothing saying those two lines must stay in
        that order.
        """
        if self.syncplay.in_group():
            log.info("Leaving the SyncPlay group: the window is closing.")
            self.syncplay.disable_sync_play(False)
        self.stop()

    def stop_and_close(self):
        log.info("stop_and_close: stopping playback")
        self.stop_for_window_close()
        if not self._mpv_alive:
            return
        try:
            self._player.keep_open = False
            self._set_force_window(False)
            self._player.command("stop")
        except _mpv_errors:
            self._handle_mpv_disconnect()
        log.info("stop_and_close: done")

    def toggle_stats(self):
        """Toggle mpv's "Playback Data" (stats.lua) overlay, tracking its
        state.

        Reached from the `i` key and from the *lua* OSC's gear sheet. The
        mpvtk HUD's gear no longer has an entry for it: that row is
        "Playback Info" now, which is ours and answers what the server is
        sending rather than what the decoder is doing (#10). Everything
        still funnels through here so ``_stats_shown`` stays truthful and
        clear_stats() can reliably put the overlay away when the library
        returns."""
        if not self._mpv_alive:
            return
        try:
            self._player.command("script-binding", "stats/display-stats-toggle")
            self._stats_shown = not self._stats_shown
        except _mpv_errors:
            self._handle_mpv_disconnect()

    # ------------------------------------------------- picture processing

    def _apply_deinterlace(self, value):
        """Write mpv's ``deinterlace``, tolerating a build without ``auto``.

        ``auto`` is mpv 0.38+; older builds reject the value outright. The
        fallback is ``no``, **never ``yes``** -- forcing deinterlacing on
        every file is not a degraded version of "deinterlace what is
        flagged", it is a different setting that softens progressive video.
        Somebody on an old mpv gets the feature they cannot have turned off
        rather than a worse one turned on, and a line in the log saying so.
        """
        if value == "auto" and self._no_deinterlace_auto:
            value = "no"               # asked and answered on this mpv
        try:
            self._player.deinterlace = value
        except _mpv_errors:
            # mpv is gone, not fussy. Diagnosing this as "your build has no
            # auto" would be wrong twice: a misleading line at INFO, and a
            # retry that swallows the disconnect instead of raising it.
            raise
        except Exception:
            if value != "auto":
                raise
            log.info("this mpv has no deinterlace=auto; leaving it off")
            self._no_deinterlace_auto = True
            self._player.deinterlace = "no"

    def _snapshot_render_pristine(self):
        """Read every property the preset-driven settings can write, from an
        mpv nothing has written to yet.

        This is the definition of "the user's own value" that the restore
        hands back: mpv's defaults as modified by the user's ``mpv.conf``,
        and nothing else.

        **Called the moment the handle exists, before anything else touches
        it**, because the shader pack gets there first in two different
        ways and both would poison this:

        - at construction, ``menu.update_player`` (and the first
          ``OSDMenu``) builds a ``VideoProfileManager``, whose ``__init__``
          re-applies the remembered profile. ``default-setting-groups``
          writes ``deband`` and, through ``profile=gpu-hq``, every property
          ``render_quality`` owns. ``shader_pack_remember`` defaults on, so
          this is the ordinary path for anyone who has picked a profile
          once;
        - per item, ``apply_for_item`` runs earlier in ``_play_media`` than
          the settings do.

        Snapshotted after either, "the user's own value" is the pack's, and
        turning the setting off hands the pack's values back over the user's
        ``mpv.conf`` -- permanently, with no profile loaded, and with
        ``deband-grain: 0`` that only made sense beside the pack's grain
        shaders.

        A property this mpv does not have is simply absent, and the restore
        skips it. That is the right answer rather than an error: an older
        build without ``hdr-contrast-recovery`` cannot have a value of it to
        put back, and the preset write for it will fail on its own terms.
        """
        from .mpv_options import PRESET_SETTINGS, preset_keys

        self._render_pristine = {}
        if self._player is None:
            return
        for key in PRESET_SETTINGS:
            for prop in preset_keys(key):
                if prop in self._render_pristine:
                    continue
                try:
                    self._render_pristine[prop] = getattr(
                        self._player, prop.replace("-", "_"))
                except Exception:
                    log.debug("this mpv has no %s to remember", prop,
                              exc_info=True)
        if not self._render_pristine:
            # Not fatal, but it means every preset becomes one-way for the
            # life of this mpv: the write guard reads an empty snapshot as
            # "never probed, write everything" while the restore reads it as
            # "nothing to put back". Near-unreachable -- the handle has just
            # been constructed -- so it is worth a line rather than a policy.
            log.warning("Could not read any picture options from this mpv; "
                        "turning a picture setting off will not restore "
                        "your own values this session.")

    def _apply_render_preset(self, key):
        """Write ``key``'s configured preset, or undo ours.

        **"Off" writes nothing until we have written something.** Every
        property these settings touch is one somebody may have set in their
        own mpv.conf, and all of them default to off -- so an off that wrote
        its idea of "not doing this" would reach out on the first item and
        undo the user's config, with no setting here to put it back.
        (``hwdec_pinned_by_config`` exists to avoid the same mistake, the
        other way round: hwdec's off is a real value that HAS to be written,
        so it cannot buy the protection by staying silent and needs a pin.)

        The undo is symmetric with the do: off restores the pristine value of
        every property any preset of this key touches -- not just this
        preset's, so switching presets before turning it off still restores
        all of them.

        ``_render_written`` is the "have we ever written this" record, and
        the second thing off has to mean: with a shader profile loaded, off
        is "I have no opinion, leave the pack's value alone" rather than
        "undo the pack". Only a key we wrote is a key we may take back.

        Why preserving ``video-sync`` alone is worse than useless:
        docs/mpv-backends.md section 6.
        """
        from .mpv_options import preset_keys, preset_props

        props = preset_props(key)
        if props:
            wrote = False
            for prop, value in props.items():
                if self._render_pristine and prop not in self._render_pristine:
                    # This mpv has no such property -- the snapshot already
                    # tried and logged it. `hdr-contrast-recovery` and
                    # `hdr-peak-percentile` are 0.37+, so this is a real
                    # build difference rather than a hypothetical one, and
                    # dropping the ONE option is better than letting it
                    # raise and take the rest of the preset (`scale`, the
                    # part that does the visible work) with it.
                    continue
                setattr(self._player, prop.replace("-", "_"), value)
                wrote = True
            # Marked ours only if something actually landed. On a build with
            # none of these properties every write is skipped, and claiming
            # the key anyway would make a later "off" hand back values we
            # never touched.
            if wrote:
                self._render_written.add(key)
            return
        if key not in self._render_written:
            return                       # never ours; not ours to turn off
        # What "off" restores TO. The pack first, where a profile is loaded
        # and sets this property: turning our setting off means "I have no
        # opinion", and with a profile on, the value it would have had
        # without us is the pack's, not mpv's default.
        #
        # Nothing else puts the pack's back. `apply_for_item` returns early
        # for an unchanged profile ("Already wearing it") -- deliberately,
        # so the pack does not rewrite its settings between every two
        # items -- so a restore to pristine here left the upscaler selected,
        # its shaders loaded, and its debanding gone for the session.
        pack = self._pack_applied()
        for prop in preset_keys(key):
            name = prop.replace("-", "_")
            if name in pack:
                value = pack[name]
            elif prop in self._render_pristine:
                value = self._render_pristine[prop]
            else:
                continue
            setattr(self._player, name, value)
        self._render_written.discard(key)

    def _pack_applied(self):
        """``{mpv property (underscored): value}`` the loaded shader profile
        is currently holding, or ``{}``.

        Read from the profile manager rather than recomputed: it records what
        it applied, and re-deriving it here would be a second implementation
        of the pack's group resolution -- which is the shape this codebase
        keeps getting wrong.
        """
        menu = getattr(self, "menu", None)
        manager = getattr(menu, "profile_manager", None) if menu else None
        if manager is None or getattr(manager, "current_profile", None) is None:
            return {}
        return getattr(manager, "applied_settings", None) or {}

    def _apply_render_presets(self):
        """Every preset-driven mpv setting, each guarded on its own.

        Separately guarded rather than one try around the loop: they are
        unrelated preferences, and an mpv that rejects one -- an older build
        without ``hdr-contrast-recovery``, say -- has no bearing on the
        others. Sharing a block means the first failure silently drops the
        rest, which is the shape the hwdec/deinterlace split in
        ``_play_media`` already avoids.
        """
        from .mpv_options import PRESET_SETTINGS

        for key in PRESET_SETTINGS:
            try:
                self._apply_render_preset(key)
            except Exception:
                log.debug("could not apply the %s setting", key, exc_info=True)

    def _apply_interpolation(self):
        """Motion interpolation, by its own name. See
        :meth:`_apply_render_preset`."""
        self._apply_render_preset("motion_interpolation")

    @synchronous("_lock")
    def set_deinterlace(self, on):
        """Force deinterlacing on or off for this session -- the playback
        HUD's gear-menu toggle. ``clear_deinterlace_override`` is the third
        state, "let the setting decide".

        Deliberately not persisted. It is the answer to one file (or one
        badly-flagged season) rather than a statement about the library, and
        ``deinterlace_auto`` is where a durable answer goes.

        ``@synchronous``, like every other ``run_action`` target that mutates
        the player: ``run_action``'s DEFERRED path holds no lock at all, so
        without this the toggle could land between ``_play_media`` reading the
        override and writing it, and the press would be silently lost.
        """
        self._deinterlace_override = bool(on)
        self.reapply_deinterlace()

    def deinterlace_forced(self):
        """Whether a session force is in effect, i.e. whether the setting is
        currently being overridden. The gear row needs it to offer a way
        BACK: with a two-state toggle and `deinterlace_auto` on, unticking
        replaced `auto` with a hard `no` for the rest of the session, so a
        later episode that IS flagged played un-deinterlaced [reviewer]."""
        return self._deinterlace_override is not None

    def deinterlace_state(self):
        """``(is_on, is_auto)`` for the gear menu's row, from MPV rather
        than from our own override -- the two differ on a build with no
        ``auto``, and what the menu has to describe is what is happening.

        ``is_on`` is ``deinterlace-active``, NOT the mode. Under ``auto``
        the mode says only "decide per file", so reading it left the row
        unticked while mpv was busily deinterlacing a flagged file -- and
        since the row toggles against what it reports, pressing it then did
        nothing visible. ``deinterlace-active`` is mpv 0.38+, which is the
        same version that introduced ``auto``: where it is missing there is
        no auto either, so the mode IS the answer and the fallback is exact
        rather than approximate.
        """
        try:
            mode = self._player.deinterlace
        except Exception:
            return False, False
        try:
            return bool(self._player.deinterlace_active), mode == "auto"
        except Exception:
            if isinstance(mode, str):
                return mode == "yes", mode == "auto"
            return bool(mode), False

    @synchronous("_lock")
    def reapply_render_presets(self):
        """Write the preset-driven settings again, now.

        For the shader pack, which writes some of the same properties and
        then puts them back. ``pack.json`` lists ``deband-default`` under
        ``default-setting-groups``, so **loading any profile turns debanding
        on and unloading one turns it off again** -- and the value unload
        restores is whatever mpv had when ``VideoProfileManager`` snapshotted
        it, which is not this setting. Without this, picking an upscaler
        mid-film and dropping it again would silently take the user's
        debanding with it for the rest of the film; ``_play_media`` would not
        put it back until the next item.

        The rule it enforces is the one the settings are documented with: a
        preset that is not "off" is the authority, and "off" means we have no
        opinion and whatever the pack (or mpv.conf) wrote stands.

        ``@synchronous`` because it arrives from two threads that are not the
        one applying settings for the next item: the shader menu's
        ``put_task`` on the action thread, and ``kb_kill_shader`` straight
        from mpv's key handler. Every one of the settings it writes is also
        written by ``_play_media``, so without the lock the two loops
        interleave and the item ends up wearing half of each -- and the
        pack's values it reads through ``_pack_applied`` are being rewritten
        by ``unload_profile`` at the same time. Blocking a key handler on
        this lock is what ``toggle_pause`` and ``toggle_fullscreen`` already
        do, and ``wait_property``'s poll thread and hard timeout are what
        keep a playback start from holding it for ever.
        """
        if self._player is None or not self._mpv_alive:
            return
        self._apply_render_presets()

    @synchronous("_lock")
    def reapply_deinterlace(self):
        """Write whatever the current override-or-setting comes to, now.

        `clear_deinterlace_override` deliberately does not touch mpv -- it
        runs on the way out of playback, where there is nothing to change.
        The gear menu clears it with something still on screen, so it needs
        the write as a separate step.

        ``@synchronous`` for the reason its sibling above is, and stated here
        too because the pair is exactly the shape this codebase gets wrong:
        the mechanism goes on one side and the second implementation is where
        the bug turns up. It reaches mpv from the HUD gateway's ``_act``,
        whose deferred path holds no lock at all.
        """
        if self._player is None or not self._mpv_alive:
            return
        try:
            from .mpv_options import deinterlace_value

            self._apply_deinterlace(
                deinterlace_value(self._deinterlace_override))
        except Exception:
            log.debug("could not re-apply deinterlacing", exc_info=True)

    def clear_deinterlace_override(self):
        """Drop the per-session force. Called on the way back to the
        library, like :meth:`clear_stats`, **and on minimize** -- closing
        the window does not go through the first one. The next thing played
        starts from the setting again, so the override lasts exactly as
        long as the watching session it was set during.

        Nothing is re-applied here: with playback over there is no picture
        to change, and ``_play_media`` writes the setting for every item.
        """
        self._deinterlace_override = None

    def clear_stats(self):
        """Hide the Playback Data overlay if it is up. Called when returning
        to the library — the overlay is ASS OSD and otherwise lingers behind
        the in-window browser after playback ends."""
        if self._stats_shown:
            self.toggle_stats()

    def stop_to_browser(self):
        """Stop playback but keep the window, so the in-window browser can
        take it back (the 'q' key while the browser is up).
        push_playstate(stopped) is what tells the browser to re-enter browse.

        That notification is also why the browse re-assert below is
        **conditional**. ``stop()`` ends in ``push_playstate(stopped=True)``,
        so the browser acts *inside* this call, and re-entering browse is only
        one of the things it may decide -- told that playback stopped with
        nothing to show, it minimizes instead. Re-asserting unconditionally
        then summoned a blank window the browser tore down a moment later.

        So the browser gets the last word. ``mpvtk_active`` is the same flag
        ``_set_force_window`` treats as the authority on who owns the window,
        and ``on_minimize`` clears it before releasing anything.
        """
        log.info("stop_to_browser: stopping playback, keeping the window")
        self.stop()
        if not self._mpv_alive or not self.mpvtk_active:
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
        if absolute and offset < 0:
            # mpv reads a negative absolute target as the END of the file,
            # not as 0 (v0.41.0: `seek -0.005 absolute+exact` on a 30s file
            # lands at 29.96 with eof-reached true). Every caller that gets
            # here with one means the start: a chapter whose container
            # timestamp is fractionally negative, which ordinary matroska
            # episodes carry, reaching us from chapter_target or straight
            # off the chapter picker. Left alone it reads as "the file
            # finished" and the queue advances (#614).
            log.debug("Clamping negative absolute seek %r to 0.", offset)
            offset = 0.0
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
                self._release_syncplay()

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
            # _mpv_alive first, and not merely the try/except. Closing the
            # window makes mpv end the file AND shut down, so this callback
            # (queued by end-file) runs on the action thread while
            # _on_shutdown_event's terminate thread is inside
            # player.terminate(). On the external backend the command is a
            # socket write and the race surfaces as BrokenPipeError, which
            # _mpv_errors catches; on in-process libmpv the handle has been
            # freed underneath us and the command is a use-after-free, which
            # is a SIGSEGV no except clause can see. _terminate_mpv clears
            # this flag before it calls terminate(), so checking it is what
            # closes the window. Found by tests/e2e/test_mpv_reopen.
            if self._mpv_alive:
                try:
                    self._player.command("stop")
                except _mpv_errors:
                    self._handle_mpv_disconnect()
                # The queue ended on its own, so nothing else will put the
                # title back. After the stop: see clear_media_title.
                self.clear_media_title()
            # Before releasing the stream, not after: this is the browser's
            # cue to come back, and the release is a blocking round trip that
            # the library screen has no reason to wait behind.
            self.push_playstate(stopped=True)
            self.release_stream(video)
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

    def _widen_queue_backwards(self, video):
        """Put the episodes *before* this one into the queue. True if it grew.

        Starting an episode from Next Up or Continue Watching builds the queue
        with ``StartItemId``, which is inclusive -- so there is nothing behind
        it to step to (#650). jellyfin-web has the same gap. Done lazily:
        nothing is fetched until someone presses previous, then once for the
        session.

        **Prepends rather than rebuilding.** The entries after the current one
        already carry their PlaylistItemIds and may have been edited
        (``insert_items``, a websocket Play command); reconstructing them from
        the server's episode list would silently discard that.

        Runs under the player lock, like every other blocking server call on
        this path, and returns False on anything unexpected -- this is a
        convenience on a keypress, and the worst honest outcome is the button
        doing nothing.
        """
        from .utils import get_seq

        if self.syncplay.is_enabled():
            # The group owns the queue. Inventing entries it has never
            # heard of is not ours to do -- request_prev is the whole
            # protocol for this, and play_prev already routes there.
            return False
        client = getattr(video, "client", None)
        if client is None:
            return False        # offline: nothing to ask
        item = getattr(video, "item", None) or {}
        if item.get("Type") != "Episode" or not item.get("SeriesId"):
            return False
        try:
            result = client.jellyfin.get_episodes(item["SeriesId"]) or {}
        except Exception:
            log.debug("could not read the series for a backwards step",
                      exc_info=True)
            return False
        ids = [e.get("Id") for e in (result.get("Items") or []) if e.get("Id")]
        try:
            index = ids.index(video.item_id)
        except ValueError:
            # The playing episode is not in its own series listing. A
            # mixed-in special, or a library that changed underneath us.
            return False
        if index <= 0:
            return False        # already the first episode
        media = video.parent
        prefix = [{"PlaylistItemId": "playlistItem{0}".format(get_seq()),
                   "Id": eid} for eid in ids[:index]]
        media.replace_queue(prefix + list(media.queue), len(prefix) + media.seq)
        log.info("Queue widened backwards by %d episode(s).", len(prefix))
        return True

    @synchronous("_lock")
    def play_prev(self):
        video = self._video
        if video is not None and not video.parent.has_prev:
            # Nothing behind us *in the queue* is not the same as nothing
            # behind us in the series. Only on an explicit press: this is a
            # server round trip, and doing it up front would put one on
            # every episode start for a button most people never touch.
            self._widen_queue_backwards(video)
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

        Reuses the abort the end-file handler sets, so the duration wait gives
        up within a poll interval instead of running out playback_timeout --
        the case worth cancelling is mpv sitting on a stalled stream for 30s.
        The cancelled flag keeps the failure path silent: the user asked for
        this.

        Deliberately takes no lock, so it is safe from the UI thread while
        _play_media holds one. Returns whether a start was in flight.

        Gated on _start_in_progress, not _loading: _loading covers only the
        mpv-side duration wait, but the start begins one PlaybackInfo round
        trip earlier -- and the spinner carrying this Cancel has been up since
        the click.
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
        # source_for_track_rules, not media_source: this now runs before
        # PlaybackInfo, where there is no negotiated source yet. It answers
        # with the negotiated one once there is.
        streams = (video.source_for_track_rules() or {}).get("MediaStreams") or []

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
            # An aid the map does not know is not a reason to abandon the
            # whole start -- and it used to be, because this indexed the map
            # directly and a KeyError here aborts _play_media halfway. The
            # index can be stale (carried over from the previous item, or
            # from a version that has since been swapped) or simply
            # unmappable (a source the server never probed reports no
            # streams at all). mpv's own default track is the right answer
            # in every one of those cases. Same shape as the subtitle branch.
            track = video.audio_seq.get(audio_uid)
            if track is None:
                log.warning("PlayerManager::audio index %s not in the stream "
                            "map %s; leaving mpv's default track.",
                            audio_uid, video.audio_seq)
            else:
                self._player.audio = track

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


    # How long playback has to sit at an unchanged position, at the end of the
    # media, before the watchdog calls it finished. Long enough not to fire on
    # ordinary rebuffering; short enough that a user does not give up first.
    STALL_FINISH_SECS = 20

    def _check_stalled_finish(self, video):
        """Whether playback has silently died at the end of a remote stream.

        A remote origin that stops delivering **without closing the
        connection** makes no statement at all: the demuxer blocks in read, so
        there is no end-file event and eof-reached stays False. With keep_open
        holding the last frame mid-queue that is indistinguishable from a
        normal hold -- the queue just stops forever.

        Deliberately requires the position to be at the END of the media, not
        merely frozen: a bare stall is far more likely to be rebuffering, and
        advancing through that would silently skip the rest of an episode.
        Items with no known duration therefore get no rescue.
        See docs/mpv-backends.md section 9.
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
            wlog.info("mpv is not running; re-creating it for playback")
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
        # is_enabled, so a HALTED group does not hold the window: nothing it
        # can send needs one (commands are recorded, not applied), and the
        # next NewPlaylist re-creates mpv anyway. On in_group() a halted group
        # would defeat idle_quit for as long as the user stayed a member.
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
        wlog.info("QUIT: %s (video=%d mpvtk=%d)", reason,
                  getattr(self, "_video", None) is not None,
                  bool(getattr(self, "mpvtk_active", False)))
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
        wlog.info("connection LOST; dead until the next play")
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


    def _terminate_mpv(self, player=None):
        wlog.info("terminating the mpv instance")
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


    def terminate(self):
        # Before stop(): stopping can tear the window down, and the size has
        # to be read while it still exists.
        self._save_window_geometry()
        # Explicitly, rather than leaving it to stop(): stop() now *halts*
        # SyncPlay so the group survives going back to the library, and there
        # is no coming back from a shutdown. The server would evict us when
        # the websocket dies, but not before the group had waited on a client
        # that is gone. in_group(), because a halted membership is exactly the
        # one stop() would not have cleaned up.
        if self.syncplay.in_group():
            self.syncplay.disable_sync_play(False)
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

    @property
    def osc_enabled(self):
        """Whether this player is supposed to have on-screen controls at all.

        The style says so: "none" is the option for no controls, replacing
        the old enable_osc switch, which only ever reached mpv's own OSC and
        so did nothing under the default style (#615). Callers that hide the
        controls temporarily -- the OSD menu -- restore to this rather than
        to a setting of their own.
        """
        return getattr(self, "_osc_style_resolved", None) != "none"

    def enable_osc(self, enabled: bool):
        if settings.mpv_ext and settings.mpv_ext_no_ovr:
            return  # Don't override user's MPV config

        if not self._mpv_alive:
            return
        style = getattr(self, "_osc_style_resolved", None)
        try:
            # Broadcast: mpv's own osc.lua and every fork of it registers
            # these, while `osc` below reaches mpv's own and nothing else.
            # So this is the whole of what "No player controls" and the
            # library browser can say to an OSC that is not ours.
            #
            # The idle logo goes unconditionally, in both directions and
            # from _init_mpv -- it has to land BEFORE the OSC's first draw,
            # not merely before the library appears. Once a fork has drawn
            # it, asking again does nothing: ModernZ parks it on a second
            # overlay that its hide path never wipes, so the mpv logo sits
            # over the library for the rest of the session (measured: an
            # untouched OSC inks 2.1% of an idle window, suppressing after
            # the draw leaves exactly 2.1%, suppressing before leaves 0%).
            # It is never handed back -- every window the shim is idle in
            # is one it draws itself.
            self.script_message("osc-idlescreen", "no", "False")
            # Visibility IS handed back, and is latched for it: restoring
            # unconditionally would also fire at construction and undo a
            # user's own `visibility=never`. docs/mpv-backends.md §12.
            if not enabled:
                self.script_message("osc-visibility", "never", "False")
                self._osc_suppressed = True
            elif self._osc_suppressed:
                self.script_message("osc-visibility", "auto", "False")
                self._osc_suppressed = False
            # Mpv's own, held off against an mpv.conf `osc=yes` -- which a
            # construction option cannot do (build_mpv_options). Not keyed
            # on `enabled`: these styles never want it back. "default" is
            # the user's option and is never written.
            if style in REPLACES_OSC and hasattr(self._player, "osc"):
                self._player.osc = False
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

    #: What mpv's own arrows do, for a remote seek on an mpv that answered
    #: nothing. mpv's documented defaults, character for character.
    _DEFAULT_SEEK = {"up": (60, False), "down": (-60, False),
                     "right": (5, False), "left": (-5, False)}

    def _seek_like_the_keyboard(self, action):
        """``(amount, exact)`` that pressing ``action`` on the keyboard
        would seek by, right now, on this mpv.

        **Asked of mpv, not of a setting.** The seek distances left the
        config in version 4, so the only place a distance lives is the
        user's input.conf -- and the keyboard reads it from there whether
        the key is mpv's own or one the shim claimed. A remote that kept
        its own number would seek a different distance from the arrow key
        beside it on the same machine, which is what it did while
        `settings.seek_*` still existed and the keyboard had stopped
        reading them.
        """
        from . import keysweep

        try:
            for key, semantic, cmd in self._swept_keys():
                if semantic != "seek" or key != action:
                    continue
                got = keysweep.action(cmd)
                if got is not None:
                    return got[1]
        except Exception:
            log.debug("could not read mpv's seek binding for %r", action,
                      exc_info=True)
        return self._DEFAULT_SEEK[action]

    def kb_seek(self, action):
        if action not in self._DEFAULT_SEEK:
            self.menu.menu_action(action)
            return
        amount, exact = self._seek_like_the_keyboard(action)
        if settings.use_web_seek:
            # Routed by sign, as the claimed-key path does: a binding says
            # which way it goes, never which arrow it was.
            back, forward = self.get_seek_times()
            amount = forward if amount > 0 else back
        self.seek(amount, exact=exact)

    # Jellyfin remote navigation (MoveUp/Select/… from a phone or web
    # client) -> mpv key names. While the mpvtk browser owns input its
    # forced nav bindings catch these; during video playback they fall
    # through to kb_seek as before.
    _NAV_KEYPRESS = {"up": "UP", "down": "DOWN", "left": "LEFT",
                     "right": "RIGHT", "ok": "ENTER", "back": "ESC",
                     # jellyfin-web's hamburger, while the library is up:
                     # the context menu of whatever is focused. That menu
                     # holds Play / Queue / Watched / Favorite / Download,
                     # so this is the whole of those actions from ten feet
                     # away, with no second UI for them.
                     "menu": "MENU"}

    # Remote commands the in-window browser answers with a real page (or,
    # for search, with the cursor in its search box). The OSD menu has
    # none of them, so for it settings still just opens the menu.
    _NAV_COMMANDS = ("home", "settings", "search")

    #: ...and the one of them that still means something over a playing
    #: video. "Go home" is a way OUT of what is playing — the browser stops
    #: playback and shows the home screen — where settings and search would
    #: be opening a library page behind a film nobody asked to leave.
    _NAV_COMMANDS_WHILE_PLAYING = ("home",)

    _MENU_ALIAS = {"settings": "home"}

    def _nav_command(self, action):
        handler = self.on_nav_command
        if handler is None or not self.mpvtk_active:
            return False
        if (not self._library_showing()
                and action not in self._NAV_COMMANDS_WHILE_PLAYING):
            return False
        try:
            return bool(handler(action))
        except Exception:
            log.debug("nav command %r failed", action, exc_info=True)
            return False

    def _nav_back(self):
        handler = self.on_nav_back
        # `_library_showing()`, NOT `_video is None`. Audio keeps `_video`
        # set and keeps the browser on screen -- that is what the
        # now-playing bar is for -- so the old test refused BACK for the
        # whole of music and audiobook playback, while the user was looking
        # straight at the library. The mouse's back button rides this (the
        # renderer routes it as a synthetic ESC), so it stopped working the
        # moment anything played; and because you could never go back,
        # FORWARD had nothing to return to and looked broken with it.
        #
        # Its sibling `_nav_command` two functions up already asks this way,
        # as do `_stats_key` and `_play_media`. See `_library_showing`.
        if handler is None or not self.mpvtk_active \
                or not self._library_showing():
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

    def _library_showing(self):
        """Whether the library is the thing on screen.

        A video PICTURE is what takes it away; **music does not**. Audio
        keeps `_video` set and keeps the browser up — that is what the
        now-playing bar is for — so `_video is None` answers "is the
        library up?" with a no while the user is looking straight at it.
        Same pairing as `_stats_key` and `_play_media`, for the same
        reason.
        """
        return self._video is None or self._current_is_audio()

    def _library_has_input(self):
        """The *library* owns input — not merely the renderer.

        A summoned playback HUD owns input too (same bindings, same
        user-data flag: it is what lets a remote's arrows drive the HUD),
        so `_mpvtk_input_active` alone answers this with a yes over a
        playing video. Anything that means one thing in a library and
        another in a player has to ask here.
        """
        return self._library_showing() and self._mpvtk_input_active()

    def _mpvtk_hud_idle(self):
        """True while the playback HUD is attached but hidden: remote
        Move*/Select should reach the renderer's summon bindings (the
        first press shows the HUD) instead of acting as seek keys.
        Back keeps its stop-to-browser meaning while hidden."""
        return self._mpvtk_userdata("user-data/mpvtk/hud")

    def menu_action(self, action):
        if action == "search":
            # Browsing, this puts the cursor in the chrome's search box.
            # During playback it is deliberately nothing: there is no
            # search surface over a video, and jellyfin-web's own player
            # does nothing with its search button either. Returning here
            # keeps it out of the OSD-menu fallback below.
            self._nav_command("search")
            return
        if action == "menu" and not self._library_has_input():
            # The hamburger with no library to point at — mid-playback
            # (summoned HUD or not), or a UI-less build. It means the
            # player's settings menu, and it toggles, exactly as the
            # kb_menu key does. Ahead of the OSD-menu branch so that a
            # second press closes that menu rather than re-showing its
            # root, and ahead of the keypress branch because a summoned
            # HUD holds the same input the library does.
            self.toggle_settings_menu()
            return
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
        elif action == "settings":
            # The cog, with no Settings page to open: mid-playback, or a
            # build with no in-window UI. The player's own settings menu —
            # the HUD's gear under mpvtk, the OSD menu otherwise. It used
            # to be `kb_seek("home")`, which under mpvtk drew the OSD menu
            # *under* the overlay bitmaps and took the arrow keys with it.
            self.toggle_settings_menu()
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
