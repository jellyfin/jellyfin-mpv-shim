"""A library grid, and a person's filmography.

Two route kinds, one page class. They share a renderer and differ only in
where their items come from and which header controls make sense, so
``PersonPage`` is a three-line subclass rather than a copy — which is what
the ``ROUTES`` table could not express (``"person": ("_load_person",
"_render_grid")`` pointed two kinds at one method and left the differences
as ``route["kind"] ==`` checks inside it).

Everything here was private to these two routes: the sort and filter bars,
the paged-grid layout, the infinite-scroll fetcher, the reload-on-change
handlers.
"""

import logging

from ...i18n import _
from ...mpvtk.widgets import (
    Box, Button, Checkbox, Column, Dropdown, Row, Spacer, Text, VScroll)
from .. import theme
from ..components import chrome
from ..tile_renderer import GRID_GAP
from .base import Page

#: Grid sort modes (label, SortBy, SortOrder) — ported from the Tk browser.
#: Only the grid and person routes sort, so they live with their page.
SORTS = [
    (_("Name"), "SortName", "Ascending"),
    (_("Date Added"), "DateCreated", "Descending"),
    (_("Release Date"), "PremiereDate", "Descending"),
    (_("Community Rating"), "CommunityRating", "Descending"),
    (_("Date Played"), "DatePlayed", "Descending"),
    (_("Play Count"), "PlayCount", "Descending"),
    (_("Runtime"), "Runtime", "Ascending"),
    (_("Critic Rating"), "CriticRating", "Descending"),
    (_("Parental Rating"), "OfficialRating", "Ascending"),
    (_("Random"), "Random", "Ascending"),
]
_LETTERS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ"

log = logging.getLogger("mpvtk_browser.pages.grid")


class GridPage(Page):
    kind = "grid"

    #: Header height (title + optional filter bar + count) so the virtualizer
    #: can map a scroll offset onto a tile row. Deliberately approximate: the
    #: window has a ±viewport margin, so a few px off is invisible there
    #: (snap_off needs the exact value instead).
    HEAD_H = 40 + 110

    # -- load --------------------------------------------------------------

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server
        parent = route["parent_id"]
        _n, sort_by, sort_order = SORTS[route.get("_sort", 0)]
        filters = route.get("_filters") or {}
        collections = bool(route.get("_collections"))

        def work():
            if collections:
                # Collections are server-wide and recursive (a BoxSet
                # can gather items from several libraries), so this is a
                # different query, not a filter on the library.
                items, total = source.get_movie_collections(
                    srv, sort_by=sort_by, sort_order=sort_order,
                    filters=filters)
            else:
                items, total = source.get_library_items(
                    srv, parent, sort_by=sort_by, sort_order=sort_order,
                    filters=filters)
            vals = route.get("_filtervals")
            if vals is None:
                try:
                    vals = source.get_filter_values(srv, parent)
                except Exception:
                    vals = {"genres": [], "years": []}
            return items, total, vals

        def done(res):
            items, total, vals = res
            route["_items"], route["_filtervals"] = items, vals
            # Random reshuffles server-side on every request, so page two is
            # drawn from a different ordering than page one: paging it yields
            # duplicates and silently skips items. Reporting the first page as
            # the whole list is what the Tk browser did, and the paginator's
            # "an empty page ends the list" rule can never fire here because a
            # reshuffle always returns something.
            route["_total"] = len(items) if sort_by == "Random" else total
            # The toggle only makes sense on a movies library, and only
            # when the source can answer it (the offline catalog can't).
            route["_collection_capable"] = (
                route.get("collection_type") == "movies"
                and hasattr(source, "get_movie_collections"))

        self.route_async(work, done, epoch)

    # -- render ------------------------------------------------------------

    def render(self, size):
        art = self.ctx.art
        tiles = art.tiles
        route = self.route
        items = route.get("_items")
        if items is None:
            return chrome.busy()
        header = self._header(items)
        geom, image_type = self._grid_shape(items)
        if self._pages.enabled():
            return self._paged_grid(size, header, geom, image_type)
        rows = header + tiles.grid_of(
            items, "grid", size, geom=geom, image_type=image_type,
            scroll_id="grid", head_h=self.HEAD_H)
        return VScroll(
            Column(rows, pad=chrome.CONTENT_PAD, gap=GRID_GAP,
                   align="stretch"), id="grid",
            flex=1,
            # Back-nav lands where you left it (see Page.parked_scroll).
            offset=self.parked_scroll("grid"),
            # Row-snap the grid: people scroll libraries fast, and a
            # quantized offset turns per-frame smear (every visible row
            # repositioned, a full 4K recomposite each frame) into stable,
            # row-aligned frames.
            snap=geom.strip_h + GRID_GAP,
            # Exact content-y of the first tile row (not the approximate
            # HEAD_H): a snap stop landing a few px short leaves the previous
            # row's caption — its year label — peeking at the top edge.
            snap_off=tiles.header_offset(header),
            on_scroll=lambda off, mx: art.scroll.on_scroll(
                "grid", off, mx,
                lambda o, m: self._on_scroll_end(o, m)),
        )

    @property
    def _pages(self):
        """The shell's Paginator. Reached through ``ctx.art`` for the same
        reason ``tiles`` and ``scroll`` are: it is a render-time service."""
        return self.ctx.art.pages

    def _header(self, items):
        route = self.route
        # Annotated: the collections Row and the filter bar join a list that
        # starts with a Text, so mypy would infer list[Text] from it.
        header: list = [Text(route.get("title", ""), size=26, bold=True)]
        if route.get("_collection_capable"):
            header.append(Row([
                Checkbox(_("Collections"), bool(route.get("_collections")),
                         id="grid-collections",
                         on_toggle=self._toggle_collections)],
                gap=10, align="center"))
        header.append(self._filter_bar())
        # The count line is redundant with the pagination bar's "of N".
        if not self._pages.enabled():
            total = route.get("_total") or 0
            header.append(Text(_("%(shown)d of %(total)d") % {
                "shown": len(items), "total": total},
                size=14, color=theme.SUBTLE_FG))
        return header

    def _filter_bar(self):
        route = self.route
        vals = route.get("_filtervals") or {}
        filters = route.get("_filters") or {}
        genres = vals.get("genres") or []
        gi = 0
        if filters.get("genre") in genres:
            gi = genres.index(filters["genre"]) + 1
        # Years come back as ints; keep them that way in the filter (the
        # offline source compares against ProductionYear directly) and only
        # stringify for display.
        years = list(vals.get("years") or [])
        yi = 0
        if filters.get("year") in years:
            yi = years.index(filters["year"]) + 1
        bar = Row([
            Dropdown("grid-sort", [s[0] for s in SORTS],
                     selected=route.get("_sort", 0), w=180,
                     on_select=lambda i, v: self._set("_sort", i)),
            Dropdown("grid-genre", [_("All Genres")] + genres, selected=gi,
                     w=180,
                     on_select=lambda i, v: self._set_filter(
                         "genre", None if i == 0 else genres[i - 1])),
            Dropdown("grid-year",
                     [_("All Years")] + [str(y) for y in years],
                     selected=yi, w=140,
                     on_select=lambda i, v: self._set_filter(
                         "year", None if i == 0 else years[i - 1])),
            Checkbox(_("Unplayed"), bool(filters.get("unplayed")),
                     id="grid-unplayed",
                     on_toggle=lambda: self._toggle_filter("unplayed")),
            Checkbox(_("Favorites"), bool(filters.get("favorite")),
                     id="grid-fav",
                     on_toggle=lambda: self._toggle_filter("favorite")),
            # Reflects and writes the GLOBAL paginated setting — a convenient
            # place to flip it, not a per-view filter.
            Checkbox(_("Paginated"), self._pages.enabled(),
                     id="grid-paginated",
                     on_toggle=lambda: self._pages.toggle(self.route,
                                                          "grid")),
            Spacer(),
            Button(_("Shuffle"), id="grid-shuffle", on_click=self._shuffle),
        ], gap=10, align="center")
        cur_letter = filters.get("letter")
        letters = Row([
            # flex + align="center" centres the glyph horizontally; a bare
            # Text is packed at the box's left edge (Box only centres on its
            # cross axis), which left every letter hugging its left border.
            Box([Text(ch, size=15, align="center", flex=1,
                      color=theme.ACCENT_FG if cur_letter == ch
                      else theme.SUBTLE_FG)],
                id="grid-l-" + ch, w=26, h=26, align="center",
                direction="row", radius=4,
                bg=theme.ACCENT if cur_letter == ch else None,
                # Lighten rather than suppress: the selected letter used to
                # take hover=None so the fill would survive the pointer, but
                # that also made it the one letter in the row that does not
                # answer the cursor — and it is still clickable (clicking it
                # clears the filter). Same rule as controls.tab_btn.
                hover={"fill": theme.ACCENT_HOVER if cur_letter == ch
                       else theme.BUTTON_BG},
                on_click=lambda c=ch: self._set_filter(
                    "letter", None if cur_letter == c else c))
            for ch in _LETTERS], gap=2, align="center")
        return Column([bar, letters], gap=8)

    def _paged_grid(self, size, header, geom, image_type="Primary"):
        """Paginated grid/person: one screenful of tiles, no scroll — the
        bottom pagination bar (drawn by the shell) moves between pages."""
        tiles = self.ctx.art.tiles
        head_h = tiles.header_offset(header)
        ps = self._pages.page_size(self.route, size, head_h, geom,
                                   chrome.CONTENT_PAD)
        page_items = self._pages.ensure(
            self.route, ps, self._page_fetcher(),
            seed=self.route.get("_items"))
        if page_items is None:
            body = [Text(_("Loading…"), size=18, color=theme.SUBTLE_FG)]
        else:
            body = tiles.grid_of(page_items, "grid", size, geom=geom,
                                 image_type=image_type)
        return Column(header + body, pad=chrome.CONTENT_PAD, gap=GRID_GAP,
                      align="stretch", flex=1)

    # -- data ---------------------------------------------------------------

    def _bound_query(self):
        """``(sort_by, sort_order, filters, person, srv)`` read NOW, on the
        loop thread. The sort/filters a page is fetched with must be the ones
        it was asked for, not whatever they are when it lands."""
        _n, sort_by, sort_order = SORTS[self.route.get("_sort", 0)]
        return (sort_by, sort_order,
                self.route.get("_filters") or {},
                self.route.get("person_id"),
                self.route.get("server") or self.ctx.server)

    def _fetch_at(self, start, limit=None, bound=None):
        """One page of results. ``bound`` is a _bound_query() tuple captured
        on the loop thread; omitted only where the caller is already on it."""
        sort_by, sort_order, filters, person, srv = bound or self._bound_query()
        source = self.ctx.source
        kw = {} if limit is None else {"limit": limit}
        if person:
            # Sort here too. It used to be read and then not passed, so page 1
            # honoured the dropdown and every page after it silently reverted
            # to SortName — duplicates and skips as the two orderings
            # interleave.
            return source.get_person_items(
                srv, person, start_index=start,
                sort_by=sort_by, sort_order=sort_order, **kw)
        if self.route.get("_collections"):
            return source.get_movie_collections(
                srv, start_index=start, sort_by=sort_by,
                sort_order=sort_order, filters=filters, **kw)
        return source.get_library_items(
            srv, self.route["parent_id"], start_index=start,
            sort_by=sort_by, sort_order=sort_order, filters=filters, **kw)

    def _on_scroll_end(self, offset, maximum):
        route = self.route
        # Bound HERE, on the loop thread, not inside the worker: the
        # sort/filters a page is fetched with must be the ones it was asked
        # for, not whatever they are when it lands. Paginator.more runs
        # `fetch` on a pool worker, so calling _bound_query() in there read
        # the route dict at landing time -- the epoch guard happens to drop
        # the stale result today, but the invariant this comment describes
        # was no longer the thing enforcing it.
        bound = self._bound_query()

        def put(r, items, total):
            r["_items"], r["_total"] = items, total

        self._pages.more(
            route, offset, maximum,
            lambda r: (r.get("_items") or [], r.get("_total") or 0),
            put, lambda start: self._fetch_at(start, bound=bound))

    def _page_fetcher(self):
        """``fetch(start, limit) -> (items, total)`` for a paginated grid or
        person route. A Random sort reshuffles server-side per request, so it
        can't be paged — page the already-loaded items in memory instead."""
        sort_by = self._bound_query()[0]
        if sort_by == "Random":
            items = self.route.get("_items") or []
            return lambda start, limit: (items[start:start + limit],
                                         len(items))
        return lambda start, limit: self._fetch_at(start, limit)

    def _grid_shape(self, items):
        """``(geom, image_type)`` for this grid, shaped by its own artwork.

        jellyfin-web's library grid asks for ``CardShape.Auto`` and lets the
        median ``PrimaryImageAspectRatio`` decide (``ItemsView.tsx:87``);
        movies come out as posters because movie posters *are* 2:3, not
        because anything says "movies are posters". Ours said it, for every
        collection type at once, which is why a Home Videos library -- 16:9
        camcorder clips with no poster art to crop -- came out portrait.

        **Computed once per route and parked.** A grid is paged, and a
        median taken per page would change the grid's shape as you scroll
        through one library. A route is one folder, so the first page's
        median is the folder's, which is the granularity the answer actually
        depends on. Cleared wherever ``_items`` is (see ``_reload``): a
        filter or a sort is a different set of items and deserves a fresh
        look.

        The fallback when *nothing* carries a ratio is square, matching web
        (``cardBuilder.js:102-104``). That case is precisely a grid of
        art-less items -- the server sets the ratio from the Primary image,
        so no image means no ratio -- and square placeholders tile better
        than tall ones.
        """
        route = self.route
        parked = route.get("_grid_shape")
        if parked is None:
            art = self.ctx.art
            parked = art.tiles.auto_geom(items, default=art.geom_square,
                                         default_type="Primary")
            route["_grid_shape"] = parked
            # Logged because the inputs are the user's artwork, so "why is
            # this library the shape it is" is otherwise unanswerable
            # without their server. Ratios, not just the verdict: a grid
            # that came out portrait because nothing carried a ratio and one
            # that came out portrait because the content really is portrait
            # want different fixes.
            if log.isEnabledFor(logging.DEBUG):
                ratios = sorted(r for r in
                                (i.get("PrimaryImageAspectRatio")
                                 for i in items or ())
                                if isinstance(r, (int, float)) and r > 0)
                log.debug(
                    "grid %s (%s): %d/%d items carry a ratio %s -> %dx%d %s",
                    route.get("title"), route.get("collection_type"),
                    len(ratios), len(items or ()),
                    ("%.3f..%.3f" % (ratios[0], ratios[-1])) if ratios
                    else "(none)",
                    parked[0].tile_w, parked[0].tile_h, parked[1])
        return parked

    # -- header actions -----------------------------------------------------

    def _reload(self):
        for k in ("_items", "_total", "_grid_shape"):
            self.route.pop(k, None)
        self.route["_loading"] = False
        self._pages.reset(self.route)
        self.ctx.nav.reload(self.route)

    def _set(self, key, value):
        self.route[key] = value
        self._reload()

    def _set_filter(self, key, value):
        self.route.setdefault("_filters", {})[key] = value
        self._reload()

    def _toggle_filter(self, key):
        f = self.route.setdefault("_filters", {})
        f[key] = not f.get(key)
        self._reload()

    def _toggle_collections(self):
        """Movies library <-> its collections, like jellyfin-web's toggle.
        Collections are server-wide and recursive, so this is a different
        query rather than a filter."""
        self.route["_collections"] = not self.route.get("_collections")
        for k in ("_items", "_total", "_loading", "_grid_shape"):
            self.route.pop(k, None)
        self.ctx.nav.reload(self.route)

    def _shuffle(self):
        srv = self.route.get("server") or self.ctx.server
        source = self.ctx.source
        parent = self.route["parent_id"]
        actions = self.ctx.actions

        def work():
            return source.get_shuffle_ids(srv, parent)

        def done(ids):
            if ids:
                actions.play_list(ids, srv, 0)

        self.ctx.run.run(work, done, self.ctx.run.epoch)


class PersonPage(GridPage):
    """A person's filmography. Same grid, different source and header.

    Sort only: genre/year/letter filters make no sense over one person's
    credits, but "newest first" very much does. That distinction used to be
    a ``route["kind"] == "person"`` branch inside the shared renderer.
    """

    kind = "person"

    HEAD_H = 40 + 46

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server
        # The repository has taken sort_by/sort_order since it was written;
        # this was the one caller that never passed them, so the dropdown had
        # nowhere to land.
        _label, sort_by, sort_order = SORTS[route.get("_sort", 0)]

        def work():
            return source.get_person_items(
                srv, route["person_id"],
                sort_by=sort_by, sort_order=sort_order)

        def done(res):
            items, total = res
            route["_items"] = items
            # Random reshuffles server-side per request, so paging it yields
            # duplicates and skips. Cap at the first page, as the grid does
            # and as Tk did — the cap lived only in the grid loader, so the
            # filmography had the corruption the cap exists to prevent.
            route["_total"] = len(items) if sort_by == "Random" else total

        self.route_async(work, done, epoch)

    def _header(self, items):
        return [Text(self.route.get("title", ""), size=26, bold=True),
                self._sort_bar()]

    def _sort_bar(self):
        """Just the sort dropdown, for routes with no filterable axes."""
        return Row([
            Text(_("Sort"), size=15, color=theme.SUBTLE_FG),
            Dropdown("person-sort", [s[0] for s in SORTS],
                     selected=self.route.get("_sort", 0), w=180,
                     on_select=lambda i, v: self._set("_sort", i)),
        ], gap=10, align="center")
