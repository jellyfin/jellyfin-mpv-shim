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
        fullscreen_disable: bool

        # Provided by siblings on the composed PlayerManager. Three, and the
        # count is the point: if it grows, the window concern is drifting back
        # into the lifecycle it was separated from.
        def _init_mpv(self) -> None: ...

        def idle_quit(self, reason: str = ...) -> None: ...

        def _handle_mpv_disconnect(self) -> None: ...

        # ReportingMixin.
        def upd_player_hide(self) -> None: ...

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
