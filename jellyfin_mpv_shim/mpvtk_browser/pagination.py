"""``Paginator`` — paging a result set too large to hold.

Step 6c's third prep. Four routes (grid, person, music, music_genre) page
their contents, in two different modes that share this machinery:

**Windowed** (``window``) is what a library grid does: the list is ``_total``
entries long from the first frame, mostly holes, and the pages covering
what is on screen are fetched as it comes into view. That is what makes the
scrollbar full-length and the drag stable — the bug in #617 was that the
scroller grew under the thumb as pages arrived.

**Infinite scroll** (``more``) appends the next chunk as the user nears the
bottom. Three views used to carry a copy of it and each learned its
invariants separately, which is why they are spelled out on the method. It
is what the routes that are not windowed still use.

**Paginated** (``ensure``/``go``/``jump``) fills one screenful at a time with
a bar at the bottom instead of a scrollbar. It keeps the current page and its
two neighbours warm so Next/Previous land instantly, and prunes to that
window so a deep library does not accumulate every page it visited.

Page state lives on the *route dict*, not here, and deliberately: the route
is what navigation keeps and throws away, so going back to a library returns
to the page you left, and leaving it frees the cache with no bookkeeping.
This class is the logic over that state.

``content_h`` is a callback rather than a method because sizing a page
requires measuring the shell's own chrome — the update banner, the download
bar, the now-playing bar — and only the shell knows which are up. That is the
one thing here that is not self-contained, and making it an explicit argument
is the honest way to say so.
"""

import logging

from ..i18n import _

log = logging.getLogger("mpvtk_browser.pagination")

#: Height of the bottom pagination bar. Here rather than on the browser
#: because page sizing subtracts it; the shell draws a bar of this height.
PAGINATION_BAR_H = 48

#: Most tiles one page may hold, whatever the window size. A cap on the
#: overlay budget, not on the layout.
PAGE_MAX = 60

#: How close to the bottom of a scroller a page request is triggered.
#: Only the append-only routes still use this; see ``window`` versus
#: ``more`` below.
PAGE_SLOP = 800

#: Items one windowed fetch asks for. The list is sized from the server's
#: total from the first frame, so this is only "what does filling one hole
#: cost": large enough that dragging the scrollbar across a library does not
#: issue a request per row, small enough to land while the user is still
#: looking at where it goes.
#:
#: It is also the repository's default page limit, deliberately: the route's
#: initial load fetches [0, limit), so an equal WINDOW_PAGE means page 0 is
#: complete and the first render asks for nothing. A smaller limit would
#: leave page 0 holed and re-fetched immediately.
WINDOW_PAGE = 100


def spread(items, total, new, start):
    """``items`` widened to ``total`` slots, with ``new`` placed at ``start``.

    A windowed list is ``total`` entries long and mostly ``None``: an item's
    index **is** its position in the library, which is what lets the grid be
    the right height and the scrollbar the right length before anything has
    been fetched. Everything downstream reads a hole as "not here yet" and
    draws a blank tile of the right size.

    A falsy ``total`` means the source did not say (or said Random, where
    what is loaded is all there is) — then this is a plain splice and the
    list keeps whatever length it had.
    """
    out = list(items or ())
    new = list(new or ())
    if total:
        # A page that no longer fits is dropped rather than extending the
        # list past the total. The clamp used to run FIRST and the
        # extension below could undo it, so a page landing after the
        # library shrank left a list thousands of slots long over a total
        # of fifty -- the header reading "50 items" above a grid of holes
        # nothing would ever fill, because window() clamps its range to
        # the total and so never asks for them.
        if start >= total:
            new = []
        elif start + len(new) > total:
            del new[total - start:]
    if start + len(new) > len(out):
        out.extend([None] * (start + len(new) - len(out)))
    for i, it in enumerate(new):
        out[start + i] = it
    if total:
        if len(out) < total:
            out.extend([None] * (total - len(out)))
        elif len(out) > total:
            del out[total:]
    return out


def enabled_from_settings():
    """The global paginate-tile-grids toggle (``settings.paginated``).

    Read live so the Settings toggle takes effect on the next frame; a
    missing key (test stand-ins) reads as off.
    """
    try:
        from ..conf import settings
        return bool(getattr(settings, "paginated", False))
    except Exception:
        return False


class Paginator:
    """Infinite-scroll and fixed-page paging over a route's result set."""

    def __init__(self, run, content_h, is_current, status, invalidate,
                 enabled, cols, set_enabled=None, forget=None):
        #: An :class:`~.async_runner.AsyncRunner`.
        self.run = run
        #: ``content_h(route, size)`` -> the vertical space this route's
        #: content actually gets, chrome already subtracted.
        self._content_h = content_h
        #: ``is_current(route)`` -- whether this route is still the screen.
        #: Paging a route the user has left is the bug most of the guards
        #: below exist for.
        self._is_current = is_current
        self._status = status
        self._invalidate = invalidate
        #: ``enabled()`` -- the global paginate-tile-grids setting, read live
        #: so the Settings toggle takes effect on the next frame.
        self.enabled = enabled
        #: ``cols(width, geom)`` -- tiles across, from the tile renderer.
        self._cols = cols
        #: ``set_enabled(bool)`` -- persist the global setting.
        self._set_enabled = set_enabled or (lambda _v: None)
        #: ``forget(*scroll_ids)`` -- drop a scroll container's remembered
        #: offset. See toggle().
        self._forget = forget or (lambda *_ids: None)

    def toggle(self, route, *scroll_ids):
        """The inline Paginated checkbox: flip and persist the GLOBAL setting.

        No reload -- the data is unchanged, only how it is presented -- but
        reset the page state so turning it on lands on page 1.

        ``scroll_ids`` are the scroll containers this view has when it is
        NOT paginated; a paginated page has none, so they are torn down and
        rebuilt across the flip and must not carry an offset over. On mpv
        >= 0.36 ``ScrollState`` gets this right on its own from the
        renderer's live snapshot -- this is what covers the older builds
        that have no ``user-data`` to answer with, where the remembered
        offset is all there is and a stale one virtualizes the returning
        grid into blank spacers.
        """
        self._set_enabled(not self.enabled())
        self.reset(route)
        if scroll_ids:
            self._forget(*scroll_ids)
        self._invalidate()

    # -- infinite scroll ---------------------------------------------------

    def more(self, route, offset, maximum, get, put, fetch, error=None):
        """One page of an infinite-scroll list.

        ``get(route)`` returns ``(items, total)``, ``put(route, items, total)``
        writes them back, and ``fetch(start_index)`` asks the source for the
        next page as ``(new_items, total)``.

        * **Only page the route that is on screen** — a scroll event can
          arrive for a view being left.
        * **``_loading`` guards re-entry**, and must not survive a failure, or
          the list never requests anything again for the rest of the session.
          (``run`` calls ``always`` regardless of epoch for this reason.)
        * **An in-range page that comes back empty ends the list.** A random
          sort that reshuffles per request, or a filter the server applies
          differently than we do, otherwise gets re-asked on every scroll
          event forever.
        * **Never page from an empty list** — that is start_index=0, i.e. the
          initial load, and the loader owns it.
        * **Never page against a list that is being replaced.** Live TV is
          the one screen that re-reads itself behind the user's back
          (``refresh_live_tv``), and that refresh rewrites ``_data`` from
          index 0. A page-in submitted alongside it computes ``start`` from a
          length that is about to change, so the merge either duplicates a
          page or skips one — permanently, since ``len >= total`` then ends
          the list early. ``_refreshing`` is that refresh's own guard; the
          deferral is symmetric, and either direction alone leaves the race.
        """
        if (not self._is_current(route) or route.get("_loading")
                or route.get("_refreshing")):
            return
        items, total = get(route)
        if not items or len(items) >= total:
            return
        if maximum - offset >= PAGE_SLOP:
            return                       # only page in near the bottom
        route["_loading"] = True
        ep = self.run.epoch
        start = len(items)

        def done(res):
            new, total2 = res
            cur, _t = get(route)
            merged = list(cur) + list(new)
            put(route, merged, total2 if new else len(merged))

        def failed(_exc):
            # The toast is about a list. Nobody asked for this page — it was
            # triggered by scrolling — so reporting it over whatever screen
            # the user moved to is noise. (An edit the user *pressed a button*
            # for is the opposite case.)
            if self._is_current(route):
                self._status(error or _("Could not load more items."))

        def clear_guard():
            route["_loading"] = False

        # clear_guard is `always`, not part of done/failed: a page dropped for
        # being stale runs neither, and a _loading left set means this route
        # never pages again — scroll to the bottom, click a tile, come back,
        # and the list is silently capped for the rest of the session.
        self.run.run(lambda: fetch(start), done, ep, on_error=failed,
                     always=clear_guard)

    # -- windowed (true virtual scroll) ------------------------------------

    def window(self, route, first, last, get, put, fetch, error=None):
        """Fetch whatever of items ``[first, last)`` is not loaded yet.

        The infinite-scroll counterpart to ``ensure``, and like ``ensure`` it
        is called from **render**: which items are visible is a question
        about geometry, and only the view can answer it. The range passed
        must be the range the view actually composites, or the grid fetches
        one window and draws another.

        ``get``/``put``/``fetch`` are ``more``'s, except that ``fetch`` takes
        ``(start, limit)`` — an arbitrary offset, not just the end of the
        list — and what comes back is spliced in at ``start`` rather than
        appended.

        **One attempt per page, per scroll.** Render is what drives this, so
        a page that re-requested on failure would issue a request per frame
        for as long as the server stayed down — and the toast it raises
        invalidates, which is a frame. So an attempt is remembered in
        ``_win_tried`` and not repeated; ``rewindow`` clears that, and the
        view calls it on a scroll. A window that failed is therefore retried
        when the user moves, which is the cadence ``more`` had, and never
        when they hold still.

        ``_win_load`` is separate and is the in-flight set: clearing
        ``_win_tried`` mid-scroll must not re-issue a request that has not
        come back yet. It is cleared in ``always``, so a page dropped for
        being stale releases it too.
        """
        if not self._is_current(route):
            return
        items, total = get(route)
        if not total:
            return
        first = max(0, min(int(first), total - 1))
        last = max(first + 1, min(int(last), total))
        tried = route.setdefault("_win_tried", set())
        load = route.setdefault("_win_load", set())
        for page in range(first // WINDOW_PAGE,
                          (last - 1) // WINDOW_PAGE + 1):
            if page in tried or page in load:
                continue
            start = page * WINDOW_PAGE
            stop = min(total, start + WINDOW_PAGE)
            if all(i < len(items) and items[i] is not None
                   for i in range(start, stop)):
                continue
            tried.add(page)
            load.add(page)
            self._window_fetch(route, page, get, put, fetch, error)

    def _window_fetch(self, route, page, get, put, fetch, error):
        ep = self.run.epoch
        start = page * WINDOW_PAGE

        def done(res):
            new, total2 = res
            cur, cur_total = get(route)
            total3 = total2 or cur_total
            put(route, spread(cur, total3, new, start), total3)

        def failed(_exc):
            if self._is_current(route):
                self._status(error or _("Could not load more items."))

        def clear():
            route.setdefault("_win_load", set()).discard(page)

        # `always`, like more()'s guard and for the same reason: a window
        # dropped for being stale runs neither done nor failed, and a page
        # left in the in-flight set is one this route can never ask for
        # again.
        self.run.run(lambda: fetch(start, WINDOW_PAGE), done, ep,
                     on_error=failed, always=clear)

    @staticmethod
    def rewindow(route):
        """Forget which windows have been *asked for* (not which are in
        flight).

        Called on a scroll, so a window that failed is retried when the user
        moves; and wherever the result set is replaced (a sort, a filter, a
        reload), because page 3's items are not page 3's items any more.
        """
        route.pop("_win_tried", None)

    # -- fixed pages -------------------------------------------------------

    def page_size(self, route, size, head_h, geom, pad):
        """Tiles per page = columns × rows that fit under the header. Rounds
        the row count DOWN so a page never overflows its slot (which would
        clip the last row or force a scroll); capped at PAGE_MAX for the
        overlay budget."""
        from .tile_renderer import GRID_GAP

        avail = self._content_h(route, size) - head_h - pad
        pitch = geom.strip_h + GRID_GAP
        rows = max(1, int((avail + GRID_GAP) // pitch)) if pitch > 0 else 1
        cols = self._cols(size[0], geom)
        return max(1, min(cols * rows, PAGE_MAX))

    @staticmethod
    def page_count(route, ps):
        total = route.get("_total")
        if not total or ps <= 0:
            return None
        return max(1, -(-total // ps))         # ceil

    def ensure(self, route, ps, fetch, seed=None):
        """Make the current page's items available at page size ``ps`` and
        return them (or None while a fetch is in flight).

        ``fetch(start, limit) -> (items, total)`` gets a page from the source;
        ``seed`` is an already-loaded head of the list (the initial-load chunk)
        used to fill page 0 without a second request."""
        if route.get("_page_size") != ps:
            route["_page_size"] = ps
            route["_pages"] = {}
            route["_page_loading"] = set()
        pages = route["_pages"]
        npages = self.page_count(route, ps)
        cur = route.get("_page") or 0
        if npages is not None:
            cur = max(0, min(cur, npages - 1))
        route["_page"] = cur
        route["_npages"] = npages
        if seed and cur == 0 and 0 not in pages and len(seed) >= ps:
            pages[0] = list(seed[:ps])
        self._fetch(route, cur, ps, fetch)
        for nb in (cur + 1, cur - 1):
            if nb >= 0 and (npages is None or nb < npages):
                self._fetch(route, nb, ps, fetch, prefetch=True)
        keep = {cur - 1, cur, cur + 1}
        for p in [p for p in pages if p not in keep]:
            pages.pop(p, None)
        return pages.get(cur)

    def _fetch(self, route, page, ps, fetch, prefetch=False):
        pages = route["_pages"]
        loading = route["_page_loading"]
        if page in pages or page in loading:
            return
        loading.add(page)
        ep = self.run.epoch
        start = page * ps

        def done(res):
            # Only if the page is still that size. A page number means
            # nothing on its own -- "page 2" is items 24..35 at ps=12 and
            # 60..89 at ps=30 -- so a job submitted before a resize (or
            # before the now-playing bar appeared, which is the same thing
            # to page_size) answers the question that WAS asked and lands on
            # the cache the answer no longer fits. The stale page then draws
            # 30 tiles into a 12-slot page and hides the ones that belong
            # there. The epoch cannot cover this: nothing navigated.
            if route.get("_page_size") != ps:
                return
            items, total = res
            # setdefault-shaped for the same reason `clear` is: `reset` may
            # have replaced these between submit and land.
            route.setdefault("_pages", {})[page] = list(items)
            if total:
                route["_total"] = total

        def failed(_exc):
            # A prefetch nobody asked for stays silent; a page the user is
            # waiting on says so (mirrors more()'s toast rule).
            if not prefetch and self._is_current(route):
                self._status(_("Could not load this page."))

        def clear():
            # setdefault, like _window_fetch's: `reset` pops the key
            # outright, so a job outstanding across a toggle would otherwise
            # raise KeyError in a callback nobody can see.
            route.setdefault("_page_loading", set()).discard(page)

        self.run.run(lambda: fetch(start, ps), done, ep,
                     on_error=failed, always=clear)

    @staticmethod
    def reset(route):
        """Drop the page cache and return to page 1. Called whenever the
        underlying result set changes (sort, filter, collections toggle, music
        tab) — page 3 of one ordering is nothing like page 3 of another."""
        for k in ("_pages", "_page_size", "_page_loading", "_npages",
                  "_win_tried", "_win_load"):
            route.pop(k, None)
        route["_page"] = 0

    def go(self, route, page):
        """Jump to a page (0-based); ``ensure`` clamps into range next frame,
        so an out-of-range target from Last/typing is harmless."""
        route["_page"] = page
        self._invalidate()

    def jump(self, route, text):
        """The page-number box: a 1-based page to go to."""
        try:
            n = int(str(text).strip())
        except (TypeError, ValueError):
            return
        self.go(route, max(0, n - 1))
