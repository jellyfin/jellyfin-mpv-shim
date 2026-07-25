"""Scroll-offset bookkeeping for virtualized lists.

Extracted from ``MpvtkBrowser`` (step 6b of ``docs/ARCHITECTURE_TARGET.md``
§3.1). Ten of the eighteen unconverted routes need this, and no page should
own it privately: the *renderer* is the authority on where a container is
scrolled, and the shell reads its live snapshot once per frame. A page
holding its own copy would drift from the thing actually drawing.

Three pieces of state, and each exists for a different failure:

``_live``
    The renderer's own offsets, read synchronously at the top of every
    ``build()``. The only value that cannot be stale, because the renderer
    clamps it to the *current* content.

``_recorded``
    The throttled ``on_scroll`` copy. A fallback for mpv < 0.36, which has no
    ``user-data`` and so cannot answer the live query at all.

``_rendered``
    The offset each container was last *re-rendered* at. This is the baseline
    the re-render threshold measures against, and it must not be the previous
    *event*: continuous sub-row scrolling arrives in many small steps, so
    comparing adjacent events lets a slow scroll drift a whole window without
    ever crossing the gap — and the virtualized rows fall out of the built
    window as blank spacers until some larger coalesced jump finally trips it.
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

    # -- per-frame ---------------------------------------------------------

    def refresh(self, app):
        """Take the renderer's live offsets. Call once at the top of
        ``build()``.

        Cleared first, so a failed read degrades to the recorded fallback
        rather than silently reusing last frame's numbers against this
        frame's content.
        """
        self._live = None
        if app is None or not hasattr(app, "scroll_offsets"):
            return
        try:
            self._live = app.scroll_offsets()
        except Exception:
            log.debug("scroll_offsets failed", exc_info=True)

    def offset(self, scroll_id):
        """Where ``scroll_id`` is scrolled to, in logical pixels."""
        live = self._live
        if live is not None and scroll_id in live:
            return float(live[scroll_id] or 0.0)
        return float(self._recorded.get(scroll_id, 0.0))

    # -- events ------------------------------------------------------------

    def on_scroll(self, scroll_id, offset, maximum, then=None):
        """Record a scroll and repaint if it has moved a window's worth.

        ``then(offset, maximum)`` runs first and unconditionally — it is how
        infinite scroll asks for the next page, and that must not be gated on
        the repaint threshold.
        """
        self._recorded[scroll_id] = offset
        if then is not None:
            then(offset, maximum)
        base = self._rendered.get(scroll_id)
        if base is None or abs(offset - base) >= self.STEP:
            self._rendered[scroll_id] = offset
            self._invalidate()

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
