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

import dataclasses
import logging

from ...i18n import _
from ...mpvtk.widgets import (
    Box, Button, Checkbox, Column, Dropdown, Row, Spacer, Text, VScroll)
from .. import theme, view_prefs
from ..components import chrome, controls
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

#: A view with nothing stored: web's defaults, which are also what the shim
#: did before any of this existed, so an untouched library is unchanged.
#: ``(value, key)`` pairs -- the key each was read from rides along so a save
#: lands where the user's web client will look for it (see ``view_prefs``).
_DEFAULT_VIEW = {
    "imageType": (view_prefs.DEFAULT_IMAGE_TYPE, None),
    "viewType": (view_prefs.GRID_VIEW, None),
    "showTitle": (True, None),
    "showYear": (True, None),
}

#: Collection types with a Genres screen. Music has its own, in the
#: music library's Genres tab, and it is a different screen: a music
#: genre has albums to draw as tiles, a video genre has nothing of
#: its own and is a heading over a row.
GENRE_LIBRARIES = frozenset({"movies", "tvshows"})

#: Collection types with a Networks screen. TV only, as in web:
#: studio metadata is where the networks are.
STUDIO_LIBRARIES = frozenset({"tvshows"})

#: Collection types with no Play All button -- see GridPage._play_all_capable.
NO_PLAY_ALL = frozenset({"tvshows"})

#: What a by-name screen lists, per collection type. Mirrors
#: LibrarySource.GENRE_ITEM_TYPES; kept here too because the button
#: builds the spec and the page should not import the repository.
GENRE_ITEM_TYPES = {"movies": "Movie", "tvshows": "Series"}

log = logging.getLogger("mpvtk_browser.pages.grid")


def _fmt_runtime(ticks):
    """``1h 42m`` for a list row, or "" with no runtime."""
    mins = int((ticks or 0) // 600000000)
    if not mins:
        return ""
    return ("%dh %dm" % (mins // 60, mins % 60) if mins >= 60
            else "%dm" % mins)


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
            # Read here rather than in render: it comes off a cached blob,
            # but "cached" is not "free" and render runs every frame.
            get_view = getattr(source, "get_view_settings", None)
            view = (get_view(srv, parent, route.get("collection_type"))
                    if get_view else dict(_DEFAULT_VIEW))
            return items, total, vals, view

        def done(res):
            items, total, vals, view = res
            route["_items"], route["_filtervals"] = items, vals
            route["_view"] = view
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
        header = self._header(items, size[0])
        if view_prefs.is_list_view(self._view("viewType")):
            return self._list_view(items, header)
        geom, image_type = self._grid_shape(items)
        labels = (bool(self._view("showTitle")),
                  bool(self._view("showYear")))
        if not labels[0]:
            # No caption means no room for one: the strip reserves
            # caption_h under every tile whether or not anything is drawn
            # there, so leaving it would just be a band of background.
            geom = dataclasses.replace(geom, caption_h=0)
        if self._pages.enabled():
            return self._paged_grid(size, header, geom, image_type, labels)
        rows = header + tiles.grid_of(
            items, "grid", size, geom=geom, image_type=image_type,
            scroll_id="grid", head_h=self.HEAD_H, labels=labels)
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

    def _header(self, items, width=0):
        route = self.route
        # Annotated: the collections Row and the filter bar join a list that
        # starts with a Text, so mypy would infer list[Text] from it.
        # The view-settings button rides the heading, trailing -- the same
        # placement the guide's does, and for the same reason: four controls
        # read once and rarely touched do not earn permanent space on a
        # filter row already carrying a sort, three filters and a shuffle.
        header: list = [Row([
            Text(route.get("title", ""), size=26, bold=True),
            Spacer(flex=1),
            controls.action_btn("settings", _("View"), "grid-viewcfg",
                                self._open_view_settings, size=16),
        ], align="center", w=max(0, (width or 0) - 2 * chrome.CONTENT_PAD))]
        header.append(self._filter_bar(width))
        # The count line is redundant with the pagination bar's "of N".
        if not self._pages.enabled():
            total = route.get("_total") or 0
            header.append(Text(_("%(shown)d of %(total)d") % {
                "shown": len(items), "total": total},
                size=14, color=theme.SUBTLE_FG))
        return header

    def _view_controls(self):
        """Buttons that LEAVE this library -- Genres, Networks.

        They lead the filter row rather than sitting among the filters,
        because they are a different kind of thing: a filter changes what
        this grid shows and these navigate somewhere else entirely. Mixing
        them in made "Collections" (which really is a filter) and "Genres"
        (which is a door) look like the same control.

        jellyfin-web reaches both through library tabs, which we do not
        have -- so leading the bar is the nearest thing to a tab strip.
        """
        route = self.route
        out = []
        if route.get("collection_type") in STUDIO_LIBRARIES:
            # Web calls this "Networks" on a TV library and offers it only
            # there, which is where studio metadata actually is.
            out.append(Button(_("Networks"), id="grid-studios",
                              icon="apartment", on_click=self._open_studios))
        if route.get("collection_type") in GENRE_LIBRARIES:
            out.append(Button(_("Genres"), id="grid-genres", icon="label",
                              on_click=self._open_genres))
        return out

    def _filter_bar(self, width=0):
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
        ] + ([
            # A filter, not a door: it swaps this library's grid for its
            # collections. So it sits with the other checkboxes rather than
            # with Genres and Networks -- after Favorites, before the
            # Paginated toggle, which is not a filter at all.
            Checkbox(_("Collections"), bool(route.get("_collections")),
                     id="grid-collections",
                     on_toggle=self._toggle_collections),
        ] if route.get("_collection_capable") else []) + [
            # The play buttons are trailing; the Spacer is what pushes them
            # away from the filters.
            Spacer(),
        ] + ([
            # Ahead of Shuffle, as jellyfin-web pairs them. Shuffle alone was
            # the whole of "play this library", which on a Home Videos folder
            # of holiday clips in date order is the one thing you do not want.
            Button(_("Play All"), id="grid-playall", on_click=self._play_all),
        ] if self._play_all_capable() else []) + [
            Button(_("Shuffle"), id="grid-shuffle", on_click=self._shuffle),
        ], gap=10, align="center")
        bar = self._fit_bar(bar, self._view_controls(), width)
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

    def _paged_grid(self, size, header, geom, image_type="Primary",
                    labels=None):
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
                                 image_type=image_type, labels=labels)
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

    def _list_view(self, items, header):
        """The library as a table rather than a grid.

        jellyfin-web's List view type. Deliberately plain -- name, year,
        runtime -- because the point of choosing it is that the artwork was
        not helping, so re-introducing art in a leading column would defeat
        it. It is also what makes the view cheap: no strips, no overlays, so
        a library of thousands costs nothing to draw.
        """
        rows = [
            {"cells": [it.get("Name") or "",
                       str(it.get("ProductionYear") or "")
                       if self._view("showYear") else "",
                       _fmt_runtime(it.get("RunTimeTicks"))],
             "item": it}
            for it in items]
        table = self.ctx.art.tiles.item_list(
            rows, "grid", on_click=lambda i: self.ctx.art.tiles.on_open(
                items[i]),
            scroll_id="grid",
            # What sits above the table inside the same Column, so the
            # window is computed against the table's own rows rather than
            # against the page. header_offset knows this Column's pad and
            # gap because it is the one the grid uses.
            head_h=self.ctx.art.tiles.header_offset(header))
        return VScroll(
            Column(header + [table], pad=chrome.CONTENT_PAD, gap=GRID_GAP,
                   align="stretch"), id="grid", flex=1,
            offset=self.parked_scroll("grid"),
            on_scroll=lambda off, mx: self.ctx.art.scroll.on_scroll(
                "grid", off, mx,
                lambda o, m: self._on_scroll_end(o, m)))

    def _sort_bar(self):
        """Just the sort dropdown, for routes with no filterable axes.

        On ``GridPage`` rather than a subclass because two of them want it
        now. The node id carries the route kind: the renderer targets by id
        and keeps per-node state against it, so two screens sharing one id
        would share a dropdown -- and the id was literally "person-sort"
        while the list route was being built on top of it.
        """
        return Row([
            Text(_("Sort"), size=15, color=theme.SUBTLE_FG),
            Dropdown("%s-sort" % self.kind, [s[0] for s in SORTS],
                     selected=self.route.get("_sort", 0), w=180,
                     on_select=lambda i, v: self._set("_sort", i)),
        ], gap=10, align="center")

    def _open_studios(self):
        route = self.route
        self.ctx.nav.navigate({
            "kind": "list",
            "server": route.get("server") or self.ctx.server,
            "title": _("Networks"),
            "list": {"type": "studios", "parent_id": route.get("parent_id"),
                     "include_item_types": GENRE_ITEM_TYPES.get(
                         route.get("collection_type")),
                     "shape": "landscape"}})

    def _open_genres(self):
        route = self.route
        self.ctx.nav.navigate({
            "kind": "genres",
            "server": route.get("server") or self.ctx.server,
            "parent_id": route.get("parent_id"),
            "collection_type": route.get("collection_type"),
            "title": _("Genres")})

    def _open_view_settings(self):
        # Pagination rides along even though it is not a view setting of
        # this library's: it is the same question ("how do I want this
        # drawn?"), asked once and then left alone, and the filter row is
        # not where you go looking for it a second time.
        self.ctx.dialogs.view_settings(
            self._view, self._set_view,
            paginated=(self._pages.enabled,
                       lambda: self._pages.toggle(self.route, "grid")))

    def _view(self, setting):
        """The stored value of one view setting, or its default.

        Per setting, not per dict: a source may answer with only the
        settings it knows about -- the offline catalog answers ``{}`` -- and
        falling to None for the rest would read as "off", blanking every
        caption rather than leaving them alone.
        """
        view = self.route.get("_view") or {}
        stored = view.get(setting)
        if stored is None:
            stored = _DEFAULT_VIEW.get(setting) or (None, None)
        return stored[0]

    def _set_view(self, setting, value):
        """Persist one view setting and redraw with it.

        Optimistic, like every other edit here: the grid changes on the next
        frame and rolls back if the server refuses. No reload -- the items
        are the same, only how they are drawn changes.
        """
        route = self.route
        view = dict(route.get("_view") or _DEFAULT_VIEW)
        previous, key = view.get(setting) or (None, None)
        if value == previous:
            return
        view[setting] = (value, key)
        route["_view"] = view
        # The parked median is only consulted when there is no override, but
        # it was computed for a different shape; drop it so returning to
        # "Primary" measures afresh.
        # The parked median was measured for a different shape.
        route.pop("_grid_shape", None)
        self.ctx.invalidate()
        server = route.get("server") or self.ctx.server
        parent = route.get("parent_id")
        ctype = route.get("collection_type")
        source = self.ctx.source
        save = getattr(source, "save_view_setting", None)
        if save is None:
            return

        def work():
            save(server, parent, ctype, setting, value, key=key)

        def failed(_exc):
            rolled = dict(route.get("_view") or {})
            rolled[setting] = (previous, key)
            route["_view"] = rolled
            route.pop("_grid_shape", None)
            self.ctx.status(_("That view setting could not be saved."))
            self.ctx.invalidate()

        self.ctx.run.run(work, lambda _r: None, self.ctx.run.epoch,
                         on_error=failed)

    def _fit_bar(self, bar, extras, width):
        """``bar`` with ``extras`` appended, or stacked under it if that
        would not fit.

        Measured rather than switched on a width constant, for the same
        reason the top bar is: what fits depends on how many controls this
        particular library has -- a movies library carries a Collections
        toggle and a Genres button that a music one does not, and the sort
        and genre dropdowns are sized to their contents.
        """
        if not extras:
            return bar
        wide = Row(extras + bar.children, gap=10, align="center")
        avail = (width or 0) - 2 * chrome.CONTENT_PAD
        if avail <= 0:
            return wide
        try:
            from ...mpvtk.layout import measure
            fits = measure(wide)[0] <= avail
        except Exception:
            log.debug("could not measure the filter bar", exc_info=True)
            fits = True
        if fits:
            return wide
        # Above, not below: these are the doors out of this library, and a
        # row of them under the filters reads as more filtering.
        return Column([Row(extras, gap=10, align="center"), bar], gap=8,
                      align="stretch")

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
        named = view_prefs.shape_for(self._view("imageType"))
        if named is not None:
            # An explicit choice beats the median outright, exactly as it
            # does in web (ItemsView.tsx:75-88) -- the whole point of the
            # setting is that the artwork got it wrong for this library.
            attr, image_type = named
            return getattr(self.ctx.art, attr), image_type
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

    def _play_all_capable(self):
        """Whether this library gets a Play All button.

        Everywhere but a TV library. A TV grid is a grid of *shows*, so
        "play all" over it means every episode of everything in name order,
        which is not a thing anyone wants and is a large queue to build by
        accident. Shuffle over the same set is fine -- a random episode is a
        perfectly good ask, and it is why Shuffle stays.

        (jellyfin-web does offer it there, on a tab that lists episodes
        rather than series. We have no such tab, so the button would mean
        something different from the one it is copying.)
        """
        return self.route.get("collection_type") not in NO_PLAY_ALL

    def _shuffle(self):
        self._queue_library(lambda source, srv, parent, _bound:
                            source.get_shuffle_ids(srv, parent))

    def _play_all(self):
        """Queue the library in the order the grid is showing it.

        The sort and the filters go to the server, not the loaded page: what
        is on screen is one screenful of an infinite scroll, and "Play All"
        that plays the first hundred is a worse answer than no button. Same
        as web, which hands its query options straight to playbackManager.
        """
        def work(source, srv, parent, bound):
            sort_by, sort_order, filters = bound[0], bound[1], bound[2]
            return source.get_play_all_ids(srv, parent, sort_by=sort_by,
                                           sort_order=sort_order,
                                           filters=filters)

        self._queue_library(work)

    def _queue_library(self, fetch_ids):
        """Shared body of Play All and Shuffle: resolve ids off the loop
        thread, then start the queue."""
        srv = self.route.get("server") or self.ctx.server
        source = self.ctx.source
        parent = self.route["parent_id"]
        actions = self.ctx.actions
        # Bound on the loop thread, like every other fetch here: the sort and
        # filters a queue is built from must be the ones the user could see
        # when they pressed the button.
        bound = self._bound_query()

        def work():
            return fetch_ids(source, srv, parent, bound)

        def done(ids):
            if ids:
                # pause_stills=False: these buttons mean "run it", and a
                # queue that opens on a photo would otherwise sit paused on
                # frame one. Clicking a single picture still opens a viewer.
                actions.play_list(ids, srv, 0, pause_stills=False)
            else:
                self.ctx.status(_("There is nothing here to play."))

        self.ctx.run.run(work, done, self.ctx.run.epoch)


class ListPage(GridPage):
    """A generic "everything of this kind" listing -- jellyfin-web's
    ``#/list?type=…``.

    The destination behind a section heading. Every row on the home and
    Live TV screens is a top-N of something (twelve upcoming films, twenty
    Next Up) with no way to see the rest, and this is the rest. One page
    rather than one per section because they differ only in the query:
    the grid, the paging, the sort and the shape are the same screen.

    A ``GridPage`` subclass for that reason, exactly as ``PersonPage`` is.
    What it replaces is ``parent_id`` -- a list is defined by a *predicate*
    (this genre, this studio, favourites, still-to-air films) rather than by
    a folder, which is the one thing the grid could not express.
    """

    kind = "list"

    HEAD_H = 40 + 46

    #: Specs whose query has no meaningful ordering to offer. Next Up is
    #: already in the server's watch order and re-sorting it by name would
    #: be actively worse; guide listings are chronological, and a programme
    #: list out of time order is not a listing of anything.
    UNSORTABLE = frozenset({"nextup", "programs", "recordings"})

    def _spec(self):
        return self.route.get("list") or {}

    def _sortable(self):
        return self._spec().get("type") not in self.UNSORTABLE

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server
        spec = self._spec()
        sort_by, sort_order = self._sort_args()

        def work():
            return source.get_list(srv, spec, sort_by=sort_by,
                                   sort_order=sort_order)

        def done(res):
            items, total = res
            route["_items"] = items
            # Same Random cap as the grid: the server reshuffles per request,
            # so paging one yields duplicates and skips.
            route["_total"] = len(items) if sort_by == "Random" else total

        self.route_async(work, done, epoch)

    def _sort_args(self):
        if not self._sortable():
            return None, None
        _label, sort_by, sort_order = SORTS[self.route.get("_sort", 0)]
        return sort_by, sort_order

    def _bound_query(self):
        sort_by, sort_order = self._sort_args()
        return (sort_by, sort_order, self.route.get("_filters") or {}, None,
                self.route.get("server") or self.ctx.server)

    def _fetch_at(self, start, limit=None, bound=None):
        sort_by, sort_order, filters, _person, srv = (
            bound or self._bound_query())
        kw = {} if limit is None else {"limit": limit}
        return self.ctx.source.get_list(
            srv, self._spec(), sort_by=sort_by, sort_order=sort_order,
            start_index=start, filters=filters, **kw)

    #: A spec may name its own shape where the artwork cannot be trusted to
    #: say it. Studios are the case: their tiles are wide logos, web forces
    #: backdrop + preferThumb for them (tvstudios.js:38-40), and most carry
    #: no PrimaryImageAspectRatio at all -- so the median would fall back to
    #: square and draw every network logo in a box.
    SHAPES = {"poster": ("geom", "Primary"),
              "landscape": ("geom_wide", "Thumb"),
              "square": ("geom_square", "Primary")}

    def _grid_shape(self, items):
        named = self.SHAPES.get(self._spec().get("shape"))
        if named is None:
            return super()._grid_shape(items)
        attr, image_type = named
        return getattr(self.ctx.art, attr), image_type

    def _header(self, items, width=0):
        header = [Text(self.route.get("title", ""), size=26, bold=True)]
        if self._sortable():
            header.append(self._sort_bar())
        if not self._pages.enabled():
            total = self.route.get("_total") or 0
            header.append(Text(_("%(shown)d of %(total)d") % {
                "shown": len(items), "total": total},
                size=14, color=theme.SUBTLE_FG))
        return header


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

    def _header(self, items, width=0):
        return [Text(self.route.get("title", ""), size=26, bold=True),
                self._sort_bar()]

