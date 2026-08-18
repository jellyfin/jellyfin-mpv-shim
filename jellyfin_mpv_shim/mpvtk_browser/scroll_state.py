"""Scroll-offset bookkeeping for virtualized lists.

Extracted from ``MpvtkBrowser`` (step 6b of ``docs/archive/ARCHITECTURE_TARGET.md``
§3.1). Ten of the eighteen unconverted routes need this, and no page should
own it privately: the *renderer* is the authority on where a container is
scrolled, and the shell reads its live snapshot once per frame. A page
holding its own copy would drift from the thing actually drawing.

Five pieces of state, and each exists for a different failure:

``_live``
    The renderer's own offsets, read synchronously at the top of every
    ``build()``. The only value that cannot be stale, because the renderer
    clamps it to the *current* content.

``_recorded``
    The throttled ``on_scroll`` copy. A fallback for mpv < 0.36, which has no
    ``user-data`` and so cannot answer the live query at all — and *only* for
    that. It is a whole-snapshot substitute, not a per-id one: consulting it
    for ids missing from a live snapshot resurrects offsets the renderer has
    deliberately dropped (see ``offset``).

``_rendered``
    The offset each container was last *re-rendered* at. This is the baseline
    the re-render threshold measures against, and it must not be the previous
    *event*: continuous sub-row scrolling arrives in many small steps, so
    comparing adjacent events lets a slow scroll drift a whole window without
    ever crossing the gap — and the virtualized rows fall out of the built
    window as blank spacers until some larger coalesced jump finally trips it.

``_pending``
    The offsets **this frame's scene is about to command** — the route's
    parked offsets, which the pages pass as ``Scroll(offset=…)``. For a
    container the renderer has not heard of yet, this outranks its silence:
    the scene is telling it where to go, so that is where it will be by the
    time anything is drawn.

``_seeded``
    The ids the renderer has answered for **since the parked snapshot was
    taken**. A restore is a one-shot, not a standing order, and this is what
    makes it one: without it a parked offset was re-applied every time its
    container left the scene and came back — a Live TV tab flip through the
    busy screen, a reconnect — undoing whatever the user had done since.

    "Since the parked snapshot was taken" is the whole of it, and the two
    ways a container's offset can vanish are what force that wording. A
    yield to playback empties the scene, but ``park`` runs on the way out,
    so the parked values ARE where the user is and have to be re-armed. A
    tab flip through the busy screen empties it too, and nothing parks, so
    the parked values are from the last navigation and re-applying them
    would undo the visit. Hence ``park`` clears this and a repaint does not.
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

        A failed read degrades to the recorded fallback rather than
        silently reusing last frame's numbers against this frame's content.

        ``route`` is the route about to be built, and it is read for one
        thing: the offsets parked on it, which its page is about to pass as
        ``Scroll(offset=…)``. See ``offset`` and ``pending``.
        """
        self._live = self._read(app)
        # A parked offset RESTORES a container, and a restore happens once.
        # Once the renderer has answered for an id, that container's position
        # is its own: re-offering the parked value would re-apply it the next
        # time the container left the scene and came back -- a Live TV tab
        # flip through the busy screen, a reconnect, an offline drop -- and
        # discard everything the user did after the restore.
        #
        # `_seeded` and not a pop from `_pending`, because `_pending` is
        # re-read from the route on every frame: a pop would last one build,
        # and the frame that matters is a later one. It is cleared by
        # `reset()` and by `park()`, which is what re-arms a restore for the
        # next visit and for the return from playback.
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

        Where the renderer has an answer, it is the only authority — its
        copy is the one clamped to the current content. What it does *not*
        answer for is a container that has only just entered the scene, and
        there are two of those, which want opposite things:

        * The container really is at the top. Tick Paginated on a scrolled
          grid and untick it, or change a sort (which drops to the busy
          screen and takes the scroll container with it): the renderer built
          a fresh container at 0. Letting ``_recorded`` fill that gap re-armed
          an offset the container no longer has, and windowed the returning
          grid around it — a screenful of blank spacers with no tiles in it.
          So ``_recorded`` is a whole-snapshot fallback for mpv < 0.36 and
          never a per-id one.
        * The container is being *restored*. Press Back onto a library you
          had scrolled to the end of: the scene we are building carries
          ``off0`` for exactly that, and the renderer applies it before it
          draws anything. Windowing that frame from its silence built the
          top of the list and then jumped to the bottom, so the screen came
          back empty and stayed empty until a scroll rebuilt it.

        ``_pending`` is what tells them apart, and it is not a memory of
        where the container *was*: it is where this frame's scene is about to
        *put* it. A container the user has since scrolled has a live entry,
        which still wins — including when that entry is 0.
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

        The same value :meth:`Page.parked_scroll` returns, read from the route
        ``refresh`` was handed. It is here for the containers a page does not
        build by hand: the home screen's carousels are one ``tile_row`` call
        each and their ids are generated per row, so the only code that can
        restore them is the shared component that builds them.

        That is not a convenience. ``offset`` answers for a container the
        renderer has not met with the parked value *because the scene is about
        to command it* -- so a parked offset that nothing restores makes that
        answer a lie for exactly one frame, and it is the frame a screen comes
        back on. The carousel drew at 0 with its page buttons derived from
        wherever it had been left, and nothing invalidated afterwards to
        correct them.
        """
        offset = self._pending.get(scroll_id)
        return float(offset) if offset else None

    # -- events ------------------------------------------------------------

    #: Slack for "is this container against an end stop".
    EDGE = 1.0

    def _edge(self, offset, maximum):
        """Which end stop this offset is against: -1 start, 1 end, 0 neither.

        Three states rather than a boolean, because the two ends are not
        interchangeable to the thing that reads this. A carousel one page
        longer than its viewport goes from the start stop to the end stop in
        a single click, and a boolean "is at an end" cannot tell those apart
        -- so the move that reverses both buttons was the one move that
        repainted neither, and the row sat at its end with Next lit and
        Previous dim until something else happened to invalidate.
        """
        if offset <= self.EDGE:
            return -1
        if offset >= maximum - self.EDGE:
            return 1
        return 0

    def on_scroll(self, scroll_id, offset, maximum, then=None,
                  edges_only=False):
        """Record a scroll and repaint if it has moved a window's worth.

        ``then(offset, maximum)`` runs first and unconditionally — it is how
        infinite scroll asks for the next page, and that must not be gated on
        the repaint threshold.

        Crossing into, out of, or straight across an end stop always
        repaints, whatever the distance: the carousel page buttons derive
        their disabled state from the offset, and the last few px of a drag
        to the end are usually well under ``STEP``, so the button that just
        became useless would otherwise stay lit until something else happened
        to invalidate.

        ``edges_only`` drops the distance rule and keeps just that one, for a
        container whose *only* offset-dependent content is at the ends. The
        home screen's carousels are the case: nothing about them is
        virtualized, so a mid-row repaint would recomposite a screenful of
        poster strips to change nothing.
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

        Called on the way out of a screen. ``reset()`` immediately after is
        what stops one view's offsets bleeding into the next under the same
        id; this is what makes leaving and coming back different from
        opening a view fresh. Restoring is the *page's* half — it passes
        ``offset=`` on the container it rebuilds — because only the page
        knows which of its scrollers it wants restored.

        Reads the renderer live rather than trusting the last frame's
        snapshot: a scroll shorter than ``STEP`` never triggered a rebuild,
        so ``_live`` can be up to that far behind at the moment of a click.

        **Only call this while the browser is on screen.** A yielded scene
        has no containers in it, and the renderer answers that with ``None``
        (an empty Lua table serialises as an array, not a map) — which is
        indistinguishable from "cannot be asked", so this falls through to
        ``_recorded``. That holds only the containers that installed a watch,
        which is a *subset*: a page's own vertical scroll has none. Parking
        it would therefore overwrite a complete snapshot with a partial one.
        ``MpvtkBrowser._park_scroll`` is guarded on ``_browsing`` for exactly
        this, and it is why ``_park_on_leaving_browse`` runs before
        ``_yield`` clears the flag rather than after.
        """
        live = self._read(app) if app is not None else self._live
        offsets = dict(live) if live is not None else dict(self._recorded)
        if offsets:
            store[self.PARK_KEY] = offsets
        else:
            store.pop(self.PARK_KEY, None)
        # These offsets are current as of now, so every container is owed a
        # restore from them again -- including the ones already on screen,
        # whose ids `refresh` has been marking as seeded all along.
        #
        # This is what makes coming back from playback work. That is not a
        # navigation: `enter_browse` rebuilds the same route, so `reset()`
        # never runs, and without this the returning grid was *windowed* at
        # the top while `off0` put the container at the bottom -- a
        # screenful of holes, which is the exact defect
        # tests/test_shell_paging.py:TestAReturningScrollContainerStartsAtTheTop
        # exists to catch.
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
