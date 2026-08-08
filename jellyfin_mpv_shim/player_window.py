"""The mpv window: who owns it, how big it is, and when it exists at all.

One window is shared by video playback and the in-window library browser, and
most of the difficulty here comes from that. The browser needs a window with
no file loaded; playback needs the same window back, at the size the user left
it, without mpv helpfully resizing it to each video.

Moved out of ``player.py`` as ``WindowMixin``, on the same terms as
``AudioMixin`` and ``ReportingMixin``: one object, one ``self``, one
``RLock``.

**More coupled than the other two, and the declarations below say how much.**
Three calls leave this module. ``set_browse_window`` and ``force_window`` can
call ``_init_mpv`` -- taking the window back may mean building a new mpv,
because a version that cannot give up its window on request has to be
restarted instead (see ``runtime_force_window_works``). ``set_browse_window``
also calls ``idle_quit``, which is the one outbound call that takes ``_lock``.
That is real coupling to the player's lifecycle, not an artefact of the split;
it was simply invisible while it lived in the same class.

**Fullscreen is here and it is ``_lock``-synchronized**, unlike everything
else in this module. It is a window property, so it belongs here, but the
lock discipline is the transport one -- worth knowing before this module is
reasoned about as a whole.
"""

import logging
import sys
from typing import TYPE_CHECKING, Any, Optional

from .conf import settings
from .utils import get_resource, synchronous

log = logging.getLogger("player")

#: The window's own log. Separate from "player" and at INFO so the whole
#: history of one window is `grep window: log.txt` -- a handful of lines, in
#: order, each naming what asked for the change.
#:
#: This exists because the state below is driven from four directions at once
#: (the browser entering and leaving browse, playback starting and stopping,
#: the tray, mpv's own OSC) and the only way to see the interleaving was
#: `mpv_log_level: debug`, which buries six interesting lines in thousands of
#: decoder ones. An unexplained window re-open is exactly the shape of problem
#: that needs the sequence and nothing else.
wlog = logging.getLogger("window")


def _caller(depth=2):
    """`module.function:line` of the code that asked for a transition.

    Which caller it was *is* the finding when a window re-opens on its own —
    the state alone cannot distinguish the browser re-entering browse mode
    from playback re-arming the window. Frames are cheap here: these
    transitions happen a handful of times per session, not per frame.
    """
    try:
        f = sys._getframe(depth)
        return "%s.%s:%d" % (f.f_globals.get("__name__", "?"),
                             f.f_code.co_name, f.f_lineno)
    except Exception:
        return "?"


# The mpvtk browser's window background. mpv paints it directly
# (background=color), so nothing has to be decoded to hold the window open.
#
# Set it through set_browse_bg(), never by assigning the name on another
# module. `player` re-exports WindowMixin from here, so `player.BROWSE_BG_HEX
# = x` looks like it works and silently does nothing: the code below resolves
# the name against THIS module's globals, so the assignment just adds an
# unread attribute to `player`. The theme's browse colour was lost that way
# from the day the theme system landed.
BROWSE_BG_HEX = "#141414"


def set_browse_bg(hexstr):
    """Set the browser's window background (an ``"#rrggbb"`` string)."""
    global BROWSE_BG_HEX
    if hexstr:
        BROWSE_BG_HEX = hexstr
    return BROWSE_BG_HEX
# mpv's own defaults, restored by browse_yield() when video takes the window
# back. Kept here so the browse background can't leak into playback.
MPV_DEFAULT_BACKGROUND = "tiles"
MPV_DEFAULT_BACKGROUND_HEX = "#000000"


class WindowMixin:
    """Window ownership, geometry and fullscreen for ``PlayerManager``."""

    if TYPE_CHECKING:
        # Owned by PlayerManager, not by this mixin.
        _player: Any
        _video: Any
        _mpv_alive: bool
        _loading: bool
        _runtime_force_window: bool
        mpvtk_active: bool
        _geometry_armed: Optional[str]
        _showing_browse_bg: bool
        _browse_bg_deferred: bool
        _colorspace_hint: Any
        _colorspace_hint_suspended: bool
        fullscreen_disable: bool
        on_decorations_changed: Any

        # Provided by siblings on the composed PlayerManager. Three, and the
        # count is the point: if it grows, the window concern is drifting back
        # into the lifecycle it was separated from.
        def _init_mpv(self) -> None: ...

        def idle_quit(self, reason: str = ...) -> None: ...

        def _handle_mpv_disconnect(self) -> None: ...

        # ReportingMixin.
        def upd_player_hide(self) -> None: ...

    #: mpv's ``--target-colorspace-hint``, spelled once. Both backends expose
    #: options as attributes with the dashes turned into underscores.
    _COLORSPACE_HINT = "target_colorspace_hint"

    def clear_media_title(self):
        """Put the window title back to the app's own, playback being over.

        The ``title`` option expands ``${media-title}`` live (see
        mpv_options), which is what lets the title follow playback without
        anything pushing updates. But ``force-media-title`` -- what
        ``_play_media`` sets -- is an *option*, not a per-file property: it
        outlives the file it was set for. Measured against mpv 0.40: after
        ``stop`` unloads the file, ``media-title`` still answers with it. So
        the window went on naming the last thing watched for the rest of the
        session, over a library screen (#647).

        **Only after the file is gone.** With one still loaded, clearing
        this does not empty ``media-title`` -- it falls back to the file's
        own metadata title, or failing that its path, which for us is a
        stream URL. Every caller therefore clears after its ``stop``
        command, which both backends complete before returning.
        """
        from .player import _mpv_errors     # per call: see the module docs

        if not self._mpv_alive:
            return
        try:
            self._player.force_media_title = ""
        except _mpv_errors:
            self._handle_mpv_disconnect()
        except Exception:
            # Never worth failing a stop over: the title is cosmetic, and
            # this runs in the middle of teardown paths that have real work
            # left to do.
            log.debug("could not clear the window title", exc_info=True)

    def suspend_colorspace_hint(self):
        """Let the window's colorspace follow the display while the UI owns it.

        mpv only revisits the swapchain's colorspace *while a video frame
        exists*. ``vo_gpu_next.c``::

            if (target_hint && frame->current) {
                ... set_colorspace_hint(p, &hint);
            } else if (!target_hint) {
                ... set_colorspace_hint(p, NULL);
            }

        ``target_hint`` is ``--target-colorspace-hint``, whose default of
        ``auto`` resolves to *true* on d3d11 (that context implements
        ``target_csp()``), so on Windows the first branch is the live one —
        and with no file loaded, ``frame->current`` is NULL and neither branch
        runs. The last hint set during playback is never withdrawn, and it is
        real swapchain state: libplacebo's d3d11 backend acts on it with
        ``SetColorSpace1`` plus a backbuffer format change.

        That is issue #605. Play something, turn Windows HDR on mid-playback
        (mpv re-hints the swapchain to PQ, correctly), stop, then turn HDR
        back off: nothing re-hints, so mpv keeps encoding the library UI as PQ
        while the display reads it as sRGB — raised blacks and clipped
        highlights. It only bites us because we are one of the few things that
        keeps an mpv window on screen with no file loaded; the UI *is* the
        idle window. Cycling HDR without ever opening the player is fine,
        which is what the report says, because the hint is never set at all.

        Parking the option at ``no`` takes the second branch instead, and
        ``pl_swapchain_colorspace_hint`` maps its NULL to sRGB — so the
        swapchain returns to 8-bit sRGB and stays there, letting Windows do
        the SDR-in-HDR conversion it does for every other desktop app. Which
        is the honest answer anyway: the library UI is sRGB content, so
        hinting the swapchain toward a video's colorspace while no video
        exists is meaningless.

        Only the browse window needs this. The OSD menu's ``force_window``
        window is torn down when the menu closes, and that destroys the
        swapchain along with the stale hint.

        Never raises, and does nothing at all on an mpv without the option
        (built without gpu-next, or too old) — the read is how we find out.
        """
        if getattr(self, "_colorspace_hint_suspended", False):
            return
        if not self._mpv_alive:
            return
        try:
            saved = getattr(self._player, self._COLORSPACE_HINT)
            setattr(self._player, self._COLORSPACE_HINT, "no")
        except Exception:
            wlog.debug("this mpv will not take %s", self._COLORSPACE_HINT,
                       exc_info=True)
            return
        self._colorspace_hint = saved
        self._colorspace_hint_suspended = True
        wlog.info("colorspace hint parked at no (was %r) <- %s",
                  saved, _caller())

    def resume_colorspace_hint(self):
        """Give the user's ``--target-colorspace-hint`` back before playback.

        Called from ``_play_media`` rather than from ``browse_yield``, which
        would read as the natural counterpart to ``set_browse_window``: yield
        is a browser gateway hook, so it misses every other way playback
        starts (a cast, the remote, the OSD menu). Playing HDR with the hint
        still parked would be a worse bug than the one being worked around,
        so the restore hangs off the one path all of them share.

        The flag is cleared only once mpv has taken the value, so a write that
        fails is retried by the next start rather than silently left parked.
        """
        if not getattr(self, "_colorspace_hint_suspended", False):
            return
        if not self._mpv_alive:
            return
        try:
            setattr(self._player, self._COLORSPACE_HINT, self._colorspace_hint)
        except Exception:
            wlog.debug("could not restore %s", self._COLORSPACE_HINT,
                       exc_info=True)
            return
        self._colorspace_hint_suspended = False
        wlog.info("colorspace hint restored to %r <- %s",
                  self._colorspace_hint, _caller())

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
            wlog.info("force_window=False SUPPRESSED (in-window UI owns it) "
                      "<- %s", _caller())
            return
        wlog.info("force_window=%s <- %s", value, _caller())
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

    # ---------------------------------------------------- still pictures

    def show_picture(self, path):
        """Display a local image file in the browse window.

        The comic reader's page. It is *played* rather than drawn: mpv
        decodes pictures already and has video-zoom/video-pan, which beats
        decoding each page with Pillow and pushing a viewport-sized bitmap
        through the overlay transport on every pan.

        **Here rather than in the gateway** because these are the same
        three properties ``set_browse_window`` owns — the browse background
        is itself "force_window with nothing loaded", and a picture is that
        state with something loaded. Splitting them would leave two places
        setting keep_open and image_display_duration with no idea about
        each other, which is how the backdrop and the picture would take
        turns overwriting one another.

        ``_showing_browse_bg`` goes False for exactly that reason: the flag
        means "the window is empty and painted", which stops being true
        here, and a later ``set_browse_window(True)`` has to know to stop
        the picture before claiming it again.
        """
        if not self._mpv_alive or self._video is not None:
            return False
        from .player import _mpv_errors     # per call: see the module docs
        try:
            # **keepaspect, first.** set_browse_window turns it OFF so the
            # library window resizes freely instead of snapping to the last
            # video's shape -- and with it off mpv stretches whatever is
            # loaded to fill the window. A comic page came out distorted,
            # and video-zoom had no visible effect at all, because a
            # stretched picture already fills the window at every zoom.
            self._player.keepaspect = True
            # An image has no duration, so mpv shows it for
            # image_display_duration (1s) and then idles.
            self._player.image_display_duration = "inf"
            self._player.keep_open = True
            self._player.command("loadfile", path, "replace")
            self._showing_browse_bg = False
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return False
        return True

    def clear_picture(self):
        """Take the picture down and go back to the painted browse window.

        Through ``set_browse_window`` rather than by stopping directly:
        that method is what decides what an empty window looks like, and
        reproducing half of it here is how the two drift.
        """
        if not self._mpv_alive or self._video is not None:
            return
        from .player import _mpv_errors     # per call: see the module docs
        try:
            self._player.video_zoom = 0.0
            self._player.video_pan_x = 0.0
            self._player.video_pan_y = 0.0
            # Back to the free-resizing library window. set_browse_window
            # does this too, but only when the browser is up -- and this
            # has to be true either way, or the next window resize snaps to
            # the shape of the comic page that just left.
            self._player.keepaspect = False
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return
        except Exception:
            wlog.debug("could not reset the picture view", exc_info=True)
        if self.mpvtk_active:
            self.set_browse_window(True)

    def set_picture_view(self, zoom=None, pan_x=None, pan_y=None):
        """Move the displayed picture: mpv's three view properties.

        ``zoom`` is mpv's own unit — a log2 exponent on top of the scale
        that fits the picture in the window — so 0 is "fits" and 1 is twice
        that. ``mpvtk_browser.gateway.picture`` turns a reading mode into
        one.
        """
        if not self._mpv_alive:
            return
        from .player import _mpv_errors     # per call: see the module docs
        try:
            if zoom is not None:
                self._player.video_zoom = float(zoom)
            if pan_x is not None:
                self._player.video_pan_x = float(pan_x)
            if pan_y is not None:
                self._player.video_pan_y = float(pan_y)
        except _mpv_errors:
            self._handle_mpv_disconnect()
        except Exception:
            wlog.debug("could not set the picture view", exc_info=True)

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
        from .player import _mpv_errors

        # getattr throughout: a trace that raises is worse than no trace, and
        # partially-built players are real (test doubles, and _init_mpv runs
        # before __init__ has finished setting all of this up).
        wlog.info(
            "browse=%s (alive=%d video=%d loading=%d mpvtk=%d bg=%d) <- %s",
            "on" if enabled else "off",
            bool(getattr(self, "_mpv_alive", False)),
            getattr(self, "_video", None) is not None,
            bool(getattr(self, "_loading", False)),
            bool(getattr(self, "mpvtk_active", False)),
            bool(getattr(self, "_showing_browse_bg", False)), _caller())
        if not self._mpv_alive:
            if not enabled:
                return
            # A window BUILT from the browse path rather than from play. It is
            # the right thing when the tray reopens the library, and a red
            # flag immediately after a minimize -- which is what an
            # unexplained blank re-open looks like from here.
            wlog.info("browse=on with no mpv: building a new one")
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
                # An idle window's colorspace has to keep tracking the
                # display, which mpv only does with the hint off (see
                # suspend_colorspace_hint) — but only once nothing is
                # playing. This method also runs with media still live: music
                # keeps the browser up, and the library can be opened over a
                # playing video. Parking the hint there would cost that video
                # its HDR passthrough, which is worse than the bug being
                # worked around. Same guard as the backdrop below, and the
                # `_loading` half is there for the same reason — _video is
                # not set until a start has already succeeded.
                if self._video is None and not self._loading:
                    self.suspend_colorspace_hint()
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
            wlog.info("releasing the window by QUITTING mpv (this mpv cannot "
                      "drop force-window at runtime)")
            self.idle_quit(reason="minimized")

    def refresh_browse_bg(self):
        """Re-apply ``BROWSE_BG_HEX`` to the live mpv window.

        The colour behind the browser is an mpv *property*, not something the
        scene paints — nothing in the UI covers the whole window, so a theme
        change that only redrew the scene would leave the old background
        showing around it. Only meaningful while the browse window is up; a
        no-op otherwise, and never fatal, because a theme change must not be
        able to take the player down with it.
        """
        try:
            if self._player is not None and getattr(
                    self, "_showing_browse_bg", False):
                self._player.background_color = BROWSE_BG_HEX
        except Exception:
            wlog.debug("could not repaint the browse background",
                       exc_info=True)

    # -- client-side decorations ------------------------------------------
    #
    # Whether the window has a title bar of its own is not an environment
    # question, it is an mpv question, and mpv answers it in `border`.
    #
    # On Wayland mpv asks for a decoration mode over zxdg_decoration_manager_v1
    # and writes the *granted* mode back into the option
    # (wayland_common.c:configure_decorations). Where the compositor does not
    # implement that protocol at all — mutter, i.e. every GNOME Wayland
    # session, which is client-side-decorations-only — mpv writes border=false
    # itself and logs "Compositor doesn't support the
    # zxdg_decoration_manager_v1 protocol". So a false `border` means "nothing
    # is drawing a title bar for this window", whoever decided that and for
    # whatever reason, which is exactly the condition these controls exist for.
    #
    # It generalises for free: X11 and win32 honour --border, so someone who
    # turned it off deliberately gets the controls too, and on a compositor
    # that does grant server-side decorations they correctly stay away. No
    # XDG_CURRENT_DESKTOP sniffing anywhere.

    #: mpv < 0.38 has no `title-bar`; a KeyError/AttributeError there means
    #: "this build only has `border`", not "there is no title bar".
    _CSD_PROPS = ("border", "title_bar")

    def _read_decorations(self):
        """True if the window has a server-drawn title bar, False if it does
        not, None if mpv could not be asked.

        Both properties have to be true: win32 can drop the title bar while
        keeping a resizable border, and a window with no title bar is one
        with nothing to drag or close by, which is the whole question.
        """
        if not self._mpv_alive or self._player is None:
            return None
        seen = False
        for prop in self._CSD_PROPS:
            try:
                value = getattr(self._player, prop)
            except Exception:
                continue        # absent on this mpv/backend; not evidence
            if value is None:
                continue
            seen = True
            if not value:
                return False
        return True if seen else None

    def window_controls_wanted(self):
        """Whether the UI should draw its own title bar right now."""
        mode = (settings.window_controls or "auto").lower()
        if mode == "never":
            return False
        # A fullscreen window has no title bar anywhere, and nothing to
        # minimize, maximize or drag. Checked ahead of "always" because
        # "always" is an answer to "does this desktop decorate my windows",
        # not a request for furniture over the top of a fullscreen video.
        try:
            if self._player is not None and self._player.fullscreen:
                return False
        except Exception:
            pass
        if mode == "always":
            return True
        # "auto" and anything unrecognised: ask mpv. An unanswerable question
        # means leave the window alone — drawing controls over a real title
        # bar is a worse failure than not drawing them under none.
        return self._read_decorations() is False

    def window_chrome_state(self):
        """``{"controls": bool, "maximized": bool}`` — everything the UI's
        own title bar has to know, in one call.

        One call because these are mpv property reads, and on the jsonipc
        backend each one is an IPC round trip: the UI must be able to take a
        snapshot on a change event rather than reading them per frame.
        """
        maximized = False
        try:
            maximized = bool(self._player.window_maximized)
        except Exception:
            pass
        return {"controls": bool(self.window_controls_wanted()),
                "maximized": maximized}

    def _on_border_change(self, _name, _value):
        """Something the UI's own title bar is drawn from changed —
        `border`, `window-maximized` or `fullscreen`. On Wayland the first of
        those is mpv reporting what the compositor granted, not us setting
        anything. Runs on mpv's event thread, so it only notifies; the UI
        re-reads and rebuilds on its own thread."""
        handler = self.on_decorations_changed
        if handler is None:
            return
        try:
            handler()
        except Exception:
            log.debug("on_decorations_changed handler failed", exc_info=True)

    def begin_window_drag(self):
        """Start an interactive window move (mpv >= 0.38).

        Normally issued from the renderer, which is holding the mouse button
        down at the time; this is the fallback path for callers that are not.
        """
        return self._window_command("begin-vo-dragging")

    def toggle_window_maximized(self):
        if not self._mpv_alive:
            return False
        try:
            self._player.window_maximized = not self._player.window_maximized
            return True
        except Exception:
            wlog.debug("could not toggle window-maximized", exc_info=True)
            return False

    def minimize_window(self):
        """Iconify via the window manager — NOT the app's own "minimize",
        which tears the window down and lives on in the tray. This is the
        title bar's button and it has to mean what that button means
        everywhere else."""
        if not self._mpv_alive:
            return False
        try:
            self._player.window_minimized = True
            return True
        except Exception:
            wlog.debug("could not minimize the window", exc_info=True)
            return False

    def _window_command(self, *args):
        from .player import _mpv_errors

        if not self._mpv_alive:
            return False
        try:
            self._player.command(*args)
            return True
        except _mpv_errors:
            self._handle_mpv_disconnect()
            return False
        except Exception:
            # begin-vo-dragging is mpv 0.38+, and a VO that does not implement
            # it errors rather than being absent. Neither is worth a traceback.
            wlog.debug("window command %r failed", args, exc_info=True)
            return False

    def raise_window(self):
        """Best-effort "bring the player window forward" — the tray's Show
        action and a second app launch both need it. Windows has a real API
        for this via pywin32; elsewhere the most we can portably do is
        un-minimize, since raising is the window manager's call."""
        from .player import win_utils

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
        from .player import _mpv_errors

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
        from .player import _mpv_errors

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
