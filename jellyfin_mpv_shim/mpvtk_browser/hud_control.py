"""``HudController`` — the playback HUD's state and the events that move it.

``hud.py`` builds the HUD's widget tree; this owns what that tree reads. The
two halves were split across two files for no reason other than history: the
builders went into ``hud.py`` when it was written, and the state plus the
handlers stayed on ``MpvtkBrowser`` because that is where ``__init__`` was.

Nothing here is browser chrome. The HUD belongs to *video playback* — the
renderer owns its summon/auto-hide lifecycle and reports it through
``on_hud``; the browser's only interest is that it must not push a HUD scene
at a renderer that is not showing one.

**State, and why each piece exists**

``shown``
    The renderer's summon flag, not ours. It is echoed here so ``build()``
    knows whether to produce a HUD scene at all.
``state``
    Latest video playstate snapshot (position, duration, chapters …), pushed
    from the player's thread via ``on_playstate``. ``None`` means no video.
``scrub`` / ``scrub_paused``
    A seek gesture in flight: the pending target in seconds, and whether
    *we* paused playback to make the position inspectable. The second flag
    is what stops a commit from resuming playback the user had paused
    themselves.
``menu`` / ``menu_anchor``
    The open settings-menu level ("root", "speed", …) and the node it hangs
    off. One level at a time.
``tc_remaining``
    Clock shows remaining rather than total. A click toggles it.

The scrub preview bubble is deliberately absent: the renderer draws it from
the trickplay tiles and mpv's chapter list, so no hover state reaches here
at all (#618).
"""

import logging

log = logging.getLogger("mpvtk_browser.hud_control")


class HudController:
    """Owns the playback HUD's state and handles its events."""

    def __init__(self, get_app, get_controller, invalidate, ctl, start_ticker):
        #: The live renderer handle. A callable rather than a value: the
        #: browser swaps it when mpv is re-created (``set_app``).
        self._get_app = get_app
        #: The ``PlayerGateway``, likewise read live.
        self._get_controller = get_controller
        self._invalidate = invalidate
        #: ``ctl(fn)`` — call ``fn(gateway)`` if there is one.
        self._ctl = ctl
        #: Start the 1s clock ticker the bar shares with the music bar.
        self._start_ticker = start_ticker
        self.reset()
        self.state = None

    @property
    def app(self):
        return self._get_app()

    @property
    def controller(self):
        return self._get_controller()

    def reset(self):
        """Forget everything the renderer told us.

        Called when a *fresh* renderer is attached: it has no HUD state, so
        keeping ours would have ``build()`` pushing a HUD scene at an idle
        renderer. ``state`` deliberately survives — it comes from the player,
        not the renderer.
        """
        self.shown = False
        self.scrub = None
        self.scrub_paused = False
        self.menu = None
        self.menu_anchor = "hud-settings"
        self.tc_remaining = False

    # -- is the HUD in play at all ----------------------------------------

    def available(self):
        """Whether yielding to video keeps the renderer attached-but-idle
        for the playback HUD (osc_style "mpvtk") instead of getting fully
        out of the way, which is what the lua OSCs need."""
        app, c = self.app, self.controller
        return (app is not None and hasattr(app, "set_hud")
                and c is not None and getattr(c, "use_hud", None) is not None
                and c.use_hud())

    def engage(self):
        """``set_hud(True)`` with everything the renderer owns attached:
        the keyboard policy (grab arrows vs. wake-key-only), the auto-hide
        delay and mode, and whether the glyphs carry their own shadow.

        Idempotent, and that matters — re-engaging is the ONLY thing that
        carries a changed setting to the renderer, so those settings apply
        without a restart."""
        opts = None
        get = getattr(self.controller, "hud_key_opts", None)
        if get is not None:
            try:
                opts = get()
            except Exception:
                opts = None
        self.app.set_hud(True, opts)

    # -- scrubbing ---------------------------------------------------------

    def scrub_change(self, v):
        if self.scrub is None:
            # gesture start: pause so the position is inspectable;
            # commit/cancel restores playback if WE paused it
            self.scrub_paused = not (self.state or {}).get("paused")
            if self.scrub_paused:
                self._ctl(lambda c: c.set_paused(True))
        self.scrub = float(v)
        self._invalidate()

    def scrub_done(self):
        self.scrub = None
        if self.scrub_paused:
            self.scrub_paused = False
            self._ctl(lambda c: c.set_paused(False))
        self._invalidate()

    def scrub_commit(self, v):
        self._ctl(lambda c: c.seek(float(v)))
        self.scrub_done()

    def scrub_cancel(self):
        self.scrub_done()

    # -- menu / events -----------------------------------------------------

    def on_skip(self):
        """The renderer's standalone idle skip button was activated."""
        self._ctl(lambda c: c.hud_action("skip-segment"))

    def open_menu(self):
        """Summon the HUD with the gear menu open (the player routes
        the kb_menu key here during playback, replacing the OSD menu
        under the in-window OSC). Pressing it again closes the menu.
        Returns True when handled."""
        if not self.available() or self.state is None:
            return False
        try:
            if self.shown and self.menu:
                self.menu = None            # kb_menu toggles
                self._invalidate()
                return True
            self.engage()                   # no-op when already engaged
            self.app.summon_hud()
            self.menu = "root"
            self.menu_anchor = "hud-settings"
            self._invalidate()
            return True
        except Exception:
            log.debug("open_hud_menu failed", exc_info=True)
            return False

    def on_hud(self, active):
        """Renderer summoned / auto-hid the playback HUD (loop thread)."""
        self.shown = bool(active)
        if self.scrub_paused:
            self.scrub_done()   # resumes playback the scrub paused
        self.scrub = None
        if not active:
            # keep a menu opened in the same beat as a summon
            # (open_menu sets it right before the hud event lands)
            self.menu = None
        if active:
            # a fresh position snapshot before the bar first paints, then
            # the shared 1s ticker keeps its clock moving
            try:
                self.controller.refresh_playstate()
            except Exception:
                log.debug("playstate refresh failed", exc_info=True)
            self._start_ticker()
        self._invalidate()
