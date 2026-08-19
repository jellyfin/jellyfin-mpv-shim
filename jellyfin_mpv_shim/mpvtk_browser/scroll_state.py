"""Scroll-offset bookkeeping for virtualized lists.

The *renderer* is the authority on where a container is scrolled, and the
shell reads its live snapshot once per frame; a page holding its own copy
would drift from the thing actually drawing, which is why none owns this
privately.

Five pieces of state, one per failure -- ``_live``, ``_recorded``,
``_rendered``, ``_pending``, ``_seeded``. What each answers, and the bug
that forced it: see docs/browser-shell.md section 7.
"""


import logging

log = logging.getLogger("mpvtk_browser.scroll_state")


class ScrollState:
    """Owns where each scroll container is, and when that warrants a repaint."""

    #: How far a view must scroll before the virtualized window is rebuilt.
    #: Small enough that the refresh lands well before the user reaches the
    #: edge of the built window.
    STEP = 120

    def __init__(self, invalidate):
        #: Wake the render loop. Called when a scroll has moved far enough to
        #: warrant rebuilding the virtualized window.
        self._invalidate = invalidate
        self._recorded = {}
        self._rendered = {}
        self._live = None
        self._pending = {}
        self._seeded = set()

    # -- per-frame ---------------------------------------------------------

    def refresh(self, app, route=None):
        """Take the renderer's live offsets. Call once at the top of
        ``build()``.

        A failed read degrades to ``_recorded`` rather than silently reusing
        last frame's numbers against this frame's content. ``route`` is read
        for one thing: the offsets parked on it, which its page is about to
        pass as ``Scroll(offset=…)``. See ``offset`` and ``pending``.
        """
        self._live = self._read(app)
        # A restore is a one-shot: once the renderer has answered for an id,
        # that container's position is its own. `_seeded` and not a pop from
        # `_pending`, which is re-read from the route every frame -- a pop
        # would last one build, and the frame that matters is a later one.
        # Cleared by `reset()` and by `park()`, never by a repaint; see
        # docs/browser-shell.md section 7.
        if self._live:
            self._seeded.update(self._live)
        parked = (route or {}).get(self.PARK_KEY) or {}
        self._pending = {scroll_id: offset
                         for scroll_id, offset in parked.items()
                         if scroll_id not in self._seeded}

    def _read(self, app):
        """The renderer's live offsets, or None when it cannot be asked.

        Split out of ``refresh`` because ``park`` needs the same read
        *without* the per-frame state. Park runs on whatever thread called
        the navigation -- the websocket thread delivering a DisplayContent, a
        remote sending GoHome -- and calling ``refresh`` there clears
        ``_pending`` out from under a ``build()`` in progress on the loop
        thread. A torn read of ``_live`` is the one-frame glitch the browser
        tolerates by design; a ``_pending`` emptied mid-build is not, because
        ``off0`` is applied to a container exactly once and the renderer has
        already seeded it at 0 by the time the next frame could correct it.
        """
        if app is None or not hasattr(app, "scroll_offsets"):
            return None
        try:
            return app.scroll_offsets()
        except Exception:
            log.debug("scroll_offsets failed", exc_info=True)
            return None

    def offset(self, scroll_id):
        """Where ``scroll_id`` is scrolled to, in logical pixels.

        Where the renderer has an answer it is the only authority -- its copy
        is the one clamped to the current content. For a container it has not
        met, ``_pending`` tells apart the two opposite cases (one really is at
        the top, one is being *restored*), and a live entry still wins,
        including when it is 0. That is also why ``_recorded`` is a
        whole-snapshot fallback for mpv < 0.36 and never a per-id one. Both
        failures: see docs/browser-shell.md section 7.
        """
        live = self._live
        if live is not None:
            if scroll_id in live:
                return float(live[scroll_id] or 0.0)
        elif scroll_id in self._recorded:
            return float(self._recorded[scroll_id])
        return float(self._pending.get(scroll_id) or 0.0)

    def pending(self, scroll_id):
        """The offset this frame's route has parked for ``scroll_id``, or None
        -- i.e. what to pass as ``Scroll(offset=...)`` to restore it.

        For the containers a page does not build by hand: the home screen's
        carousels are one ``tile_row`` call each with ids generated per row,
        so only the shared component that builds them can restore them. Not
        a convenience -- a parked offset nothing restores makes ``offset``'s
        answer a lie for exactly one frame, and it is the frame a screen
        comes back on; see docs/browser-shell.md section 7.
        """
        offset = self._pending.get(scroll_id)
        return float(offset) if offset else None

    # -- events ------------------------------------------------------------

    #: Slack for "is this container against an end stop".
    EDGE = 1.0

    def _edge(self, offset, maximum):
        """Which end stop this offset is against: -1 start, 1 end, 0 neither.

        Three states rather than a boolean: a carousel one page longer than
        its viewport goes from one stop to the other in a single click, and
        the move that reverses *both* page buttons is the one a boolean
        cannot see. See docs/browser-shell.md section 7.
        """
        if offset <= self.EDGE:
            return -1
        if offset >= maximum - self.EDGE:
            return 1
        return 0

    def on_scroll(self, scroll_id, offset, maximum, then=None,
                  edges_only=False):
        """Record a scroll and repaint if it has moved a window's worth.

        ``then(offset, maximum)`` runs first and unconditionally -- infinite
        scroll's next-page request must not be gated on the repaint
        threshold. Crossing into, out of, or straight across an end stop
        always repaints whatever the distance, and ``edges_only`` keeps only
        that rule. What each guards: see docs/browser-shell.md section 7.
        """
        self._recorded[scroll_id] = offset
        if then is not None:
            then(offset, maximum)
        base = self._rendered.get(scroll_id)
        moved = not edges_only and abs(offset - (base or 0)) >= self.STEP
        if (base is None or moved
                or self._edge(offset, maximum)
                != self._edge(base, maximum)):
            self._rendered[scroll_id] = offset
            self._invalidate()

    # -- leaving and coming back -------------------------------------------

    #: Key the parked offsets are stored under on a route dict.
    PARK_KEY = "_scroll"

    def park(self, store, app=None):
        """Remember where every container is, on ``store`` (a route dict).

        Called on the way out of a screen; ``reset()`` immediately after is
        what stops one view's offsets bleeding into the next under the same
        id. Restoring is the *page's* half -- only it knows which of its
        scrollers it wants restored. Reads the renderer live rather than
        trusting ``_live``, which a scroll shorter than ``STEP`` leaves
        behind because it never triggered a rebuild.

        **Only call this while the browser is on screen.** A yielded scene
        holds no containers, the renderer answers ``None`` -- indistinguishable
        from "cannot be asked" -- and this falls through to ``_recorded``,
        which holds only the containers that installed a watch, so a partial
        snapshot overwrites a complete one. That is why ``_park_scroll`` is
        guarded on ``_browsing`` and ``_park_on_leaving_browse`` runs before
        ``_yield`` clears it; see docs/browser-shell.md section 7.
        """
        live = self._read(app) if app is not None else self._live
        offsets = dict(live) if live is not None else dict(self._recorded)
        if offsets:
            store[self.PARK_KEY] = offsets
        else:
            store.pop(self.PARK_KEY, None)
        # Clearing this re-arms every restore, including for the containers
        # already on screen -- which is what makes coming back from playback
        # work, since `enter_browse` rebuilds the same route and `reset()`
        # never runs. Why park clears it and a repaint does not: see
        # docs/browser-shell.md section 7.
        self._seeded.clear()

    @classmethod
    def parked(cls, store, scroll_id):
        """Where ``scroll_id`` was when this route was last left, or None.

        A classmethod because it reads nothing but the route dict — the
        parked offsets travel with the route, which is what makes them
        survive the ``reset()`` that clears everything else.
        """
        return (store.get(cls.PARK_KEY) or {}).get(scroll_id)

    def forget(self, *scroll_ids):
        """Drop specific containers' offsets, leaving the rest alone.

        For an in-place content swap where the container id stays the same
        but what it holds does not — switching a music tab keeps the
        "music-grid" id while replacing every row, and a stale offset would
        virtualize the wrong window and draw a screenful of blanks.
        """
        for scroll_id in scroll_ids:
            self._recorded.pop(scroll_id, None)
            self._rendered.pop(scroll_id, None)

    def reset(self):
        """Forget every offset. Called on a route change.

        Container ids are per-*view* ("grid", "playlist", …) rather than per
        route, so without this a deep scroll in one library carried into the
        next view opened under the same id. The renderer clamps its own offset
        to the new, shorter content; our copy did not — so virtualization
        windowed rows far past the end and the view rendered empty, showing
        "7 items" in the header with nothing below it.
        """
        self._recorded.clear()
        self._rendered.clear()
        # A new screen may legitimately restore the same container ids, so
        # what the renderer confirmed about the last one says nothing here.
        self._seeded.clear()
