"""Telling the outside world what is playing.

Two audiences, one job. Jellyfin gets session/progress/stop reports over the
network; the browser's now-playing bar gets a compact snapshot through the
``on_playstate`` callback. Both are *outbound publication*, both run on
threads that are not the one driving playback, and both share the rule that a
report must never disturb what it is reporting on.

Moved out of ``player.py`` as ``ReportingMixin``. Like ``AudioMixin`` this is
a mixin rather than an owned object, so ``self`` keeps meaning what it meant
and the single ``RLock`` keeps covering what it covered.

**Its own lock.** ``_tl_lock``, not ``_lock``. Server round trips happen under
it, and ``_lock`` is held for the whole of a playback start -- sharing one
would put a progress post behind a load and a load behind a progress post.
The reports themselves are handed to ``SessionReporter`` rather than sent
inline, because they used to sit on the advance path between episodes.

Never drain the reporter while holding ``_tl_lock``: ``_session_playing_safe``
takes it on the worker.

**One thing this module deliberately does not do: touch ``pause_ignore``.**

   It used to. ``get_timeline_options`` set it to mpv's live pause state,
   which gave a flag that means "the pause value we just commanded" a second,
   contradictory meaning -- and the sample was taken twenty-five lines and
   four mpv property reads before it was written, so it could be badly stale.
   A stale sample landing on a fresh guard makes the next genuine local pause
   or unpause compare equal in ``_on_pause_change`` and get swallowed: the
   local player changes state and the SyncPlay group is never told. Fixed by
   removing the write; ``tests/test_syncplay_pause_ignore.py`` reproduces it
   deterministically and will fail if it comes back.

   The remaining SyncPlay defects found in the same trace -- including an
   ABBA inversion between ``_lock`` and ``_tl_lock`` that this module is one
   half of -- are written up in ``docs/SYNCPLAY_FINDINGS.md``.
"""

import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from .i18n import _
from .utils import none_fallback, synchronous

log = logging.getLogger("player")


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


def _discord_on():
    """Whether Discord Rich Presence came up.

    The flag is player.py's -- it is set there only if the rich_presence
    import succeeded -- so inside a true branch the hooks are importable.
    Read per call rather than bound at import, for the same reason the
    backend globals are (see player_audio).
    """
    from .player import discord_presence

    return discord_presence


class ReportingMixin:
    """Session reporting and now-playing publication for ``PlayerManager``."""

    if TYPE_CHECKING:
        # Owned by PlayerManager, not by this mixin. Listed so the coupling
        # has a length: four pieces of state written, nine read, two methods
        # called. It was five until `pause_ignore` came off the list -- which
        # is the argument for keeping it.
        _player: Any
        _video: Any
        _reporter: Any
        _session_ready: Any
        _hud_skip: Any
        syncplay: Any
        start_time: Optional[float]
        repeat_mode: str
        on_playstate: Any
        last_seek: Any
        should_send_timeline: bool
        _last_playback_position: Any
        _last_offline_record: float

        def _finished_at_eof(self, video, playback_time) -> bool: ...

        def _handle_mpv_disconnect(self) -> None: ...

    def push_playstate(self, stopped=False):
        """Feed the browser's now-playing bar a compact snapshot on each
        playback state change. Never raises and never re-enters MPV's lock — a
        bar refresh must never disturb playback. A ``stopped`` payload tells the
        bar to hide."""
        from .player import _mpv_errors

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
                # A still image. The HUD hides its scrubber and time
                # readout for one: mpv reports a 5s "duration" from
                # --image-display-duration, which is real -- that is when
                # the next photo arrives -- but scrubbing within it means
                # nothing, and a progress bar crawling across a photo reads
                # as a video about to end.
                "is_photo": item.get("Type") == "Photo",
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
        from .player import _mpv_errors

        # PlaylistItemId is dynamically generated. A more stable Id will be used
        # if queue manipulation is added as a feature.
        # self._video can be nulled at any moment by another thread (stop,
        # mpv disconnect) — take one snapshot and use only the local from
        # here on. Callers must handle a None return.
        if video is None:
            video = self._video
        if video is None:
            return None
        if getattr(video, "is_photo", False) or video.playback_info is None:
            # A photo never went through PlaybackInfo -- it has no media
            # source, no play session and nothing to report progress
            # against. Reporting one anyway would also put every picture
            # you looked at into Continue Watching.
            #
            # The playback_info check is the belt: anything else that
            # reaches here without one is equally unreportable, and this
            # used to be an AttributeError three frames deep in stop().
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
        # NOT `self.pause_ignore = pause`. That flag means "the pause value we
        # just commanded" -- set_paused records it so mpv's echo of our own
        # change is recognised and not announced to SyncPlay as a local pause.
        # Writing mpv's live state here gave it a second, contradictory
        # meaning, and the sample was taken twenty-five lines and four mpv
        # property reads earlier, so it could be badly stale by now.
        # Overwriting a fresh guard with a stale sample makes the next genuine
        # local pause or unpause compare equal and get swallowed: the player
        # changes state and the group is never told. See
        # tests/test_syncplay_pause_ignore.py.
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
        if _discord_on():
            try:
                from .rich_presence import send_presence
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
        from .player import _mpv_errors

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

        if _discord_on():
            try:
                from .rich_presence import clear_presence
                clear_presence()
            except Exception:
                log.error("Could not clear Discord Rich Presence.", exc_info=True)

    def release_stream(self, video):
        """Free ``video``'s server-side stream without blocking the caller.

        ``terminate_transcode`` is one or two blocking HTTP calls, and every
        teardown that reaches it is on a path the window is waiting on:
        ``stop()`` runs inline on the browser's loop thread whenever the
        player's lock happens to be free (run_action's fast path), and
        ``finished_callback`` holds the browser in the finished video until it
        returns. Against a server that is unreachable rather than refusing,
        those calls hang for the full socket timeout — which is exactly when
        the UI most obviously wedges, and it wedged *after* the picture was
        gone, so there was nothing on screen to explain it.

        The session reporter is the right worker rather than a fresh thread:
        one FIFO, so the release still lands in submission order with the stop
        report it accompanies, and ``terminate()`` drains it on the way out —
        so a live TV tuner is still freed by quitting the app.

        Callers that must know the stream is gone before asking for another
        one keep calling ``terminate_transcode`` directly; ``Video`` does that
        against its own source in ``get_playback_url``, which is what keeps
        re-resolving one item ordered. What is no longer ordered is stopping
        live TV and tuning a *different* channel: on a single-tuner box the
        close now has to win a race it used to be ahead of by construction.
        It is submitted before the browser has even repainted the library, so
        it has the whole of the user's navigation to land in.
        """
        if video is None:
            return
        self._reporter.submit(video.terminate_transcode, "terminate_transcode")

    def upd_player_hide(self):
        video = self._video
        if video:
            self._player.keep_open = video.parent.has_next

    # Best-effort stop report for a video whose mpv is already gone; options
    # are built from bookkeeping, not player properties. Routed through
    # send_timeline_stopped so the webview and Discord presence cleanup run
    # like any other stop.
    def _report_stopped_offline(self, video):
        if getattr(video, "is_photo", False) or video.playback_info is None:
            # The second site of the same rule as get_timeline_options: a
            # photo never went through PlaybackInfo, so there is no play
            # session to free and no stop to report -- and reporting one
            # would put every picture looked at into Continue Watching.
            # Only this site is reached on a daemon thread with nothing
            # catching it, so the missing guard showed up as a bare
            # TypeError traceback whenever the window was closed on a photo.
            log.debug("no playback info to report a stop against")
            return
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
        # Same worker as the stop report above, so the two still arrive in
        # this order — and this path runs while the window is being torn
        # down, which is not the moment to block on an unreachable server.
        self.release_stream(video)
