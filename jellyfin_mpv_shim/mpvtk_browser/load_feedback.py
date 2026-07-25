"""``LoadFeedback`` — what the user sees between pressing play and a picture.

Starting playback is seconds of work: build a Media, ask the server for
PlaybackInfo, hand the file to mpv, wait for it to report in. This owns the
two screens covering that gap and the state behind them.

**Why this is not browser chrome.** Both scenes are full-window and replace
the page entirely; they are the *video* UI standing in for a picture that has
not arrived. The browser's interest is only that the window handoff happens
at the right moment — see ``hand_off``.

**What stayed on the shell, and why.** ``_arm_spinner`` is thread mechanics:
it takes the daemon slot, waits on the shutdown event and releases the slot
early. Those live with the browser's other pollers, so the shell keeps the
thread and this owns the *policy* it runs (``spinner_due``). ``_start`` and
``_yield`` stay too — they are window-handoff, not feedback.

**The two failure presentations are deliberate.** A failure that owns the
window gets the full-screen knock-out: there is nothing behind it and the
user has no other context to lose. One that does not — audio, which keeps the
library on screen — gets a toast, because a knock-out would yank the user out
of a page they are still using over one failed track.
"""

import logging
import time

from ..i18n import _
from ..mpvtk.widgets import Busy, Button, Column, Row, Spacer, Text
from . import theme

log = logging.getLogger("mpvtk_browser.load_feedback")


class LoadFeedback:
    """The loading spinner and the playback-failure screen."""

    # A load faster than this never shows the spinner, so a snappy start does
    # not flash one up and straight back out. Short on purpose: the point is
    # only to clear the common instant start, and anything slower than about
    # half a second already reads as a hang worth acknowledging. The case the
    # spinner exists for — a file that was never qtfaststart'ed, so mpv sits
    # there relocating the moov atom — runs orders of magnitude longer.
    SPINNER_DELAY = 0.5

    def __init__(self, get_controller, invalidate, status, is_browsing,
                 enter_browse, hand_off, arm):
        self._get_controller = get_controller
        self._invalidate = invalidate
        self._status = status
        #: Live read of the browser's ``_browsing`` flag.
        self._is_browsing = is_browsing
        #: Take the window back and show the library (or, headless, the cast
        #: screen — which is why this is not a bare flag flip).
        self._enter_browse = enter_browse
        #: Yield the window to video. Called once playback reports in, for a
        #: start that held the window to draw the spinner.
        self._hand_off = hand_off
        #: Ask the shell to run the spinner timer. See the module docstring.
        self._arm = arm

        #: The in-flight load: ``{"title", "owns_window", "at"}`` or None.
        self.starting = None
        #: The last failure's info dict, or None. Read by ``build()``.
        self.error = None

    @property
    def controller(self):
        return self._get_controller()

    # -- lifecycle ---------------------------------------------------------

    def begin(self, title):
        """A start the user just asked for.

        Called the instant they click, rather than only from the player's
        on_load_start hook: that hook waits on the PlaybackInfo round trip,
        which is itself seconds of the wait the spinner exists to cover. The
        hook still matters for playback started remotely, which never passes
        through here.
        """
        self.starting = {"title": title, "owns_window": True,
                         "at": time.time()}
        self._arm()

    def on_load_start(self, info):
        """Player hook: a file is being loaded into mpv.

        Called from a foreign thread (pool worker, websocket, or action
        thread), so this only writes state and invalidates.
        """
        # The player's title is the better one (it carries the "(Transcode)"
        # suffix), but keep the click-time title when it has none rather than
        # blanking a spinner that was already naming the item.
        prev = self.starting or {}
        title = (info or {}).get("title") or prev.get("title") or ""
        # Latched, not re-read at failure time: stop() on the failure path
        # pushes a stopped playstate that returns us to browse, so reading
        # _browsing when the error lands would always say "browsing" and
        # downgrade a video failure to a toast.
        owns = prev.get("owns_window")
        if owns is None:
            owns = not self._is_browsing()
        # Keep the original click time: this hook lands one PlaybackInfo round
        # trip after the user pressed play, and restarting the clock here
        # would push the spinner out by that much again.
        self.starting = {"title": title, "owns_window": bool(owns),
                         "at": prev.get("at") or time.time()}
        self.error = None
        self._arm()
        self._invalidate()

    def on_load_error(self, info):
        """Player hook: the load failed. Foreign thread — write, don't draw.

        The error is state rather than a dialog call because that helper is
        loop-thread only; ``build()`` renders it on the next frame.
        """
        info = dict(info or {})
        starting, self.starting = self.starting or {}, None
        owns_window = starting.get("owns_window")
        if owns_window is None:
            owns_window = not self._is_browsing()
        if not owns_window:
            self.error = None
            self._status(self.error_text(info))   # invalidates
            return
        self.error = info
        self._invalidate()

    def clear(self):
        """Drop the loading/error screens once playback actually reports in.

        This is also the handoff: a video start held the window to draw the
        spinner instead of yielding (see the shell's ``_start``), so the
        yield it skipped happens here, now that there is a picture to yield
        to. Audio never took the window (``_browsing`` stays set), so it has
        nothing to hand over.
        """
        if self.starting is None and self.error is None:
            return
        handoff = self.starting is not None and not self._is_browsing()
        self.starting = None
        self.error = None
        if handoff:
            self._hand_off()   # invalidates
        else:
            self._invalidate()

    def reset(self):
        """Forget both without any window handoff."""
        self.starting = None
        self.error = None

    # -- spinner policy ----------------------------------------------------

    def spinner_due(self):
        """Whether the in-flight load has been slow enough to show it."""
        # One read: clear() can drop `starting` from another thread between
        # two of them.
        starting = self.starting
        if starting is None:
            return False
        started = starting.get("at")
        if started is None:
            return True                 # no timestamp: don't hide it forever
        return (time.time() - started) >= self.SPINNER_DELAY

    def due_in(self):
        """Seconds until the spinner is due, or None when nothing is loading.

        The shell's timer waits against this in a loop rather than sleeping a
        flat SPINNER_DELAY: the timer slot holds one thread, so a start that
        begins while one is pending has its own arm dropped. A flat sleep
        would then fire early against the new load, find it not yet due, and
        schedule nothing further — leaving that load with no spinner however
        long it ran.
        """
        starting = self.starting
        if starting is None:
            return None
        return self.SPINNER_DELAY - (time.time() - (starting.get("at") or 0))

    # -- scenes ------------------------------------------------------------

    @staticmethod
    def error_text(info):
        headline = (_("Timed out loading this item")
                    if info.get("timed_out") else _("Could not play this item"))
        title, detail = info.get("title"), info.get("detail")
        if title:
            headline = "%s: %s" % (headline, title)
        return "%s — %s" % (headline, detail) if detail else headline

    def loading_scene(self, size):
        """Spinner shown from play intent until duration arrives.

        The window has already yielded to video by this point (the yield
        leaves the renderer attached in HUD mode), so this is the video UI
        standing in for a picture that has not started yet — not a browser
        page. It replaces the empty scene the yield used to leave, which
        meant a load looked identical to a silent failure for up to
        playback_timeout, and made the player UI appear to flash in only once
        duration landed.

        Busy animates renderer-side, so holding this on screen for a 30s
        stall costs no repaints from here.
        """
        w, h = size
        title = (self.starting or {}).get("title") or ""
        rows = [Busy(w=52, h=52)]
        if title:
            rows.append(Text(title, size=22, bold=True, wrap=True,
                             align="center", w=min(760, max(280, w - 160))))
        rows.append(Text(_("Loading…"), size=16, color=theme.SUBTLE_FG))
        rows.append(Button(_("Cancel"), id="load-cancel-start",
                           on_click=self.cancel_loading))
        return Column(
            [Spacer(flex=1),
             Column(rows, gap=18, align="center"),
             Spacer(flex=1)],
            w=w, h=h, align="center", bg=theme.WINDOW_BG,
        )

    def error_scene(self, size):
        """Full-screen playback failure, with the retries worth offering.

        Retry-with-transcode is deliberately a separate button rather than
        something automatic: transcoding is expensive for the server, and an
        unexpected one is a signal something is wrong rather than a fix to
        apply silently.
        """
        w, h = size
        err = self.error or {}
        title = err.get("title") or ""
        detail = err.get("detail")
        headline = (_("Timed out loading this item")
                    if err.get("timed_out") else _("Could not play this item"))
        rows = [Text(headline, size=28, bold=True)]
        if title:
            rows.append(Text(title, size=20, wrap=True,
                             w=min(760, max(280, w - 160))))
        if detail:
            rows.append(Text(str(detail), size=15, color=theme.SUBTLE_FG,
                             wrap=True, w=min(760, max(280, w - 160))))
        buttons = [Button(_("Retry"), id="load-retry",
                          on_click=lambda: self.retry(False))]
        if err.get("can_transcode"):
            buttons.append(Button(_("Retry with Transcode"),
                                  id="load-retry-transcode",
                                  on_click=lambda: self.retry(True)))
        buttons.append(Button(_("Cancel"), id="load-cancel",
                              on_click=self.cancel_failed))
        rows.append(Row(buttons, gap=10, justify="center"))
        return Column(
            [Spacer(flex=1),
             Column(rows, gap=16, align="center"),
             Spacer(flex=1)],
            w=w, h=h, align="center", bg=theme.WINDOW_BG,
        )

    # -- buttons -----------------------------------------------------------

    def cancel_loading(self):
        """Abandon a load that is still in flight.

        The player aborts the duration wait rather than letting it run out
        playback_timeout, so this actually stops within a poll interval —
        the case worth cancelling is precisely the one where mpv sits on a
        stalled stream for the full 30s.
        """
        self.starting = None
        self.error = None
        # Abort first, then take the window back: the player is what actually
        # stops the load, and doing it in this order leaves no window where
        # we have returned to browse while the start is still running.
        cancel = getattr(self.controller, "cancel_load", None)
        if cancel is not None:
            try:
                cancel()
            except Exception:
                log.error("could not cancel the load", exc_info=True)
        self._enter_browse()

    def retry(self, force_transcode):
        """Re-attempt the failed start. The controller queues the replay onto
        the action thread, so this returns immediately and the loop thread
        keeps drawing."""
        err = self.error or {}
        self.error = None
        # Straight to the loading screen: the retry is dispatched, and leaving
        # the error up until the player reports back reads as a dead button.
        self.starting = {"title": err.get("title") or "",
                         "owns_window": True, "at": time.time()}
        self._arm()
        self._invalidate()
        retry = getattr(self.controller, "retry_playback", None)
        if retry is None:
            return
        try:
            retry(force_transcode)
        except Exception:
            log.error("could not retry playback", exc_info=True)
            self.starting = None
            self._status(_("Playback could not be started."))
            self._invalidate()

    def cancel_failed(self):
        """Give up on the failed item and take the window back.

        enter_browse (not a bare ``_browsing`` flip) because it also re-takes
        the window from the player and, in headless, lands on the cast screen
        — the only page that exists there.
        """
        self.error = None
        self.starting = None
        self._enter_browse()
