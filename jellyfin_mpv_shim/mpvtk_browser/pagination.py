"""``Paginator`` — paging a result set too large to hold.

Step 6c's third prep. Four routes (grid, person, music, music_genre) page
their contents, in two different modes that share this machinery:

**Infinite scroll** (``more``) appends the next chunk as the user nears the
bottom. Three views used to carry a copy of it and each learned its
invariants separately, which is why they are spelled out on the method.

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
PAGE_SLOP = 800


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
        """
        if not self._is_current(route) or route.get("_loading"):
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
            items, total = res
            route["_pages"][page] = list(items)
            if total:
                route["_total"] = total

        def failed(_exc):
            # A prefetch nobody asked for stays silent; a page the user is
            # waiting on says so (mirrors more()'s toast rule).
            if not prefetch and self._is_current(route):
                self._status(_("Could not load this page."))

        def clear():
            route["_page_loading"].discard(page)

        self.run.run(lambda: fetch(start, ps), done, ep,
                     on_error=failed, always=clear)

    @staticmethod
    def reset(route):
        """Drop the page cache and return to page 1. Called whenever the
        underlying result set changes (sort, filter, collections toggle, music
        tab) — page 3 of one ordering is nothing like page 3 of another."""
        for k in ("_pages", "_page_size", "_page_loading", "_npages"):
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
