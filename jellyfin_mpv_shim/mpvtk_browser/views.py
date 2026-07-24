"""The main content routes.

Home, grid (a library), detail, series, season and search, plus the
detail-page pieces: track pickers, action buttons and the media-info line.

State on ``self``: none of its own — every view keeps its data in the route
dict and every mutation ends with ``invalidate()``. Handlers here run on
the loop thread and must capture route state *before* dispatching async
work; reading ``self.route`` inside the callback races navigation.
"""

import logging

from ..i18n import _
from ..mpvtk.scaling import px
from ..mpvtk.widgets import (
    Box,
    Busy,
    Button,
    Checkbox,
    Column,
    Dropdown,
    Icon,
    Row,
    Spacer,
    Text,
    VScroll,
)
from . import components, home_sections, theme
from .components import chrome, controls, detail

log = logging.getLogger("mpvtk_browser.views")

# Grid sort modes (label, SortBy, SortOrder) — ported from the Tk browser.
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


class ViewsMixin:

    # Content-area primitives now live in components/chrome.py; these stay as
    # thin aliases until every caller is a Page taking them from its context.
    _error = staticmethod(chrome.error)
    _paragraph = staticmethod(chrome.paragraph)

    def _body_w(self, w):
        return chrome.body_width(w, self.CONTENT_PAD)


    # kind -> (loader, renderer) method names. Merged into
    # one dispatch table by core's _routes().
    ROUTES = {
        "grid": ("_load_grid", "_render_grid"),
        "person": ("_load_person", "_render_grid"),
    }

    def _square_geom(self, items):
        return self.tiles.square_geom(items)

    def _render_grid(self, route, size):
        items = route.get("_items")
        if items is None:
            return self._busy()
        header = [Text(route.get("title", ""), size=26, bold=True)]
        if route["kind"] == "grid":
            if route.get("_collection_capable"):
                header.append(Row([
                    Checkbox(_("Collections"),
                             bool(route.get("_collections")),
                             id="grid-collections",
                             on_toggle=lambda: self._toggle_collections(
                                 route))], gap=10, align="center"))
            header.append(self._grid_filter_bar(route))
            # The count line is redundant with the pagination bar's "of N".
            if not self._paginated():
                total = route.get("_total") or 0
                header.append(Text(_("%(shown)d of %(total)d") % {
                    "shown": len(items), "total": total},
                    size=14, color=theme.SUBTLE_FG))
        elif route["kind"] == "person":
            # Sort only. The full filter bar is gated on kind == "grid" and
            # person routes are "person", so a filmography had no ordering
            # control at all — genre/year/letter filters make no sense over
            # one person's credits, but "newest first" very much does.
            header.append(self._sort_bar(route))
        # Header height (title + optional filter bar + count) so the
        # virtualizer can map a scroll offset onto a tile row. Deliberately
        # approximate: the window has a ±viewport margin, so a few px off is
        # invisible there (snap_off below needs the exact value instead).
        head_h = 40 + (110 if route["kind"] == "grid" else 0) \
            + (46 if route["kind"] == "person" else 0)
        geom = self._square_geom(items) or self.geom
        if self._paginated():
            return self._paged_grid(route, size, header, geom)
        rows = header + self._grid_of(
            items, "grid", size, geom=geom,
            scroll_id="grid", head_h=head_h)
        return VScroll(
            Column(rows, pad=self.CONTENT_PAD, gap=self.GRID_GAP,
                   align="stretch"), id="grid",
            flex=1,
            # Row-snap the grid: people scroll libraries fast, and a
            # quantized offset turns per-frame smear (every visible row
            # repositioned, a full 4K recomposite each frame) into stable,
            # row-aligned frames.
            snap=geom.strip_h + self.GRID_GAP,
            # Exact content-y of the first tile row (not the approximate
            # head_h): a snap stop landing a few px short leaves the previous
            # row's caption — its year label — peeking at the top edge.
            snap_off=self._header_offset(header),
            on_scroll=lambda off, mx: self._on_scroll(
                "grid", off, mx,
                lambda o, m: self._on_grid_scroll(route, o, m)),
        )

    def _sort_bar(self, route):
        """Just the sort dropdown, for routes with no filterable axes."""
        return Row([
            Text(_("Sort"), size=15, color=theme.SUBTLE_FG),
            Dropdown("person-sort", [s[0] for s in SORTS],
                     selected=route.get("_sort", 0), w=180,
                     on_select=lambda i, v: self._set_grid("_sort", route, i)),
        ], gap=10, align="center")

    def _grid_filter_bar(self, route):
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
                     on_select=lambda i, v: self._set_grid("_sort", route, i)),
            Dropdown("grid-genre", [_("All Genres")] + genres, selected=gi,
                     w=180,
                     on_select=lambda i, v: self._set_grid_filter(
                         route, "genre", None if i == 0 else genres[i - 1])),
            Dropdown("grid-year",
                     [_("All Years")] + [str(y) for y in years],
                     selected=yi, w=140,
                     on_select=lambda i, v: self._set_grid_filter(
                         route, "year", None if i == 0 else years[i - 1])),
            Checkbox(_("Unplayed"), bool(filters.get("unplayed")),
                     id="grid-unplayed",
                     on_toggle=lambda: self._toggle_grid_filter(
                         route, "unplayed")),
            Checkbox(_("Favorites"), bool(filters.get("favorite")),
                     id="grid-fav",
                     on_toggle=lambda: self._toggle_grid_filter(
                         route, "favorite")),
            # Reflects and writes the GLOBAL paginated setting — a convenient
            # place to flip it, not a per-view filter.
            Checkbox(_("Paginated"), self._paginated(), id="grid-paginated",
                     on_toggle=lambda: self._toggle_paginated()),
            Spacer(),
            Button(_("Shuffle"), id="grid-shuffle",
                   on_click=lambda: self._grid_shuffle(route)),
        ], gap=10, align="center")
        cur_letter = filters.get("letter")
        letters = Row([
            # flex + align="center" centres the glyph horizontally; a bare
            # Text is packed at the box's left edge (Box only centres on its
            # cross axis), which left every letter hugging its left border.
            Box([Text(ch, size=15, align="center", flex=1,
                      color=theme.ACCENT_FG if cur_letter == ch
                      else theme.SUBTLE_FG)],
                id="grid-l-" + ch, w=26, h=26, align="center", direction="row",
                radius=4, bg=theme.ACCENT if cur_letter == ch else None,
                hover=None if cur_letter == ch else {"fill": theme.BUTTON_BG},
                on_click=lambda c=ch: self._set_grid_filter(
                    route, "letter", None if cur_letter == c else c))
            for ch in _LETTERS], gap=2, align="center")
        return Column([bar, letters], gap=8)

    def _reload_grid(self, route):
        for k in ("_items", "_total"):
            route.pop(k, None)
        route["_loading"] = False
        self._reset_pagination(route)
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def _set_grid(self, key, route, value):
        route[key] = value
        self._reload_grid(route)

    def _set_grid_filter(self, route, key, value):
        route.setdefault("_filters", {})[key] = value
        self._reload_grid(route)

    def _toggle_grid_filter(self, route, key):
        f = route.setdefault("_filters", {})
        f[key] = not f.get(key)
        self._reload_grid(route)

    def _toggle_paginated(self):
        """The inline Paginated checkbox: flip and persist the GLOBAL setting.
        No reload — the data is unchanged, only how it's presented — but reset
        the page state so turning it on lands on page 1."""
        self._config().set_setting("paginated", not self._paginated())
        self._reset_pagination(self.route)
        self.invalidate()

    def _grid_shuffle(self, route):
        srv = route.get("server") or self.server
        ep = self._epoch

        def work():
            return self.source.get_shuffle_ids(srv, route["parent_id"])

        def done(ids):
            if ids:
                self._play_list(ids, srv, 0)
        self.run_async(work, done, ep)

    def _on_grid_scroll(self, route, offset, maximum):
        # Read on the loop thread, before dispatch: the sort/filters must be
        # the ones the page was asked for, not whatever they are when it lands.
        _n, sort_by, sort_order = SORTS[route.get("_sort", 0)]
        filters = route.get("_filters") or {}
        person = route.get("person_id")

        def fetch(start):
            srv = route.get("server") or self.server
            if person:
                # Sort here too. It was read three lines up and then not
                # passed, so page 1 honoured the dropdown and every page
                # after it silently reverted to SortName — duplicates and
                # skips as the two orderings interleave.
                return self.source.get_person_items(
                    srv, person, start_index=start,
                    sort_by=sort_by, sort_order=sort_order)
            if route.get("_collections"):
                return self.source.get_movie_collections(
                    srv, start_index=start, sort_by=sort_by,
                    sort_order=sort_order, filters=filters)
            return self.source.get_library_items(
                srv, route["parent_id"], start_index=start, sort_by=sort_by,
                sort_order=sort_order, filters=filters)

        def put(r, items, total):
            r["_items"], r["_total"] = items, total

        self._page_more(
            route, offset, maximum,
            lambda r: (r.get("_items") or [], r.get("_total") or 0),
            put, fetch)

    def _grid_page_fetcher(self, route):
        """``fetch(start, limit) -> (items, total)`` for a paginated grid or
        person route. Sort/filters are bound now, on the loop thread (as
        _on_grid_scroll does). A Random sort reshuffles server-side per
        request, so it can't be paged — page the already-loaded items in
        memory instead."""
        _n, sort_by, sort_order = SORTS[route.get("_sort", 0)]
        filters = route.get("_filters") or {}
        person = route.get("person_id")
        srv = route.get("server") or self.server
        if sort_by == "Random":
            items = route.get("_items") or []
            return lambda start, limit: (items[start:start + limit], len(items))

        def fetch(start, limit):
            if person:
                return self.source.get_person_items(
                    srv, person, start_index=start, limit=limit,
                    sort_by=sort_by, sort_order=sort_order)
            if route.get("_collections"):
                return self.source.get_movie_collections(
                    srv, start_index=start, limit=limit, sort_by=sort_by,
                    sort_order=sort_order, filters=filters)
            return self.source.get_library_items(
                srv, route["parent_id"], start_index=start, limit=limit,
                sort_by=sort_by, sort_order=sort_order, filters=filters)
        return fetch

    def _paged_grid(self, route, size, header, geom):
        """Paginated grid/person: one screenful of tiles, no scroll — the
        bottom pagination bar (build) moves between pages."""
        head_h = self._header_offset(header)
        ps = self._page_size(route, size, head_h, geom)
        page_items = self._ensure_page(
            route, ps, self._grid_page_fetcher(route),
            seed=route.get("_items"))
        if page_items is None:
            body = [Text(_("Loading…"), size=18, color=theme.SUBTLE_FG)]
        else:
            body = self._grid_of(page_items, "grid", size, geom=geom)
        return Column(header + body, pad=self.CONTENT_PAD, gap=self.GRID_GAP,
                      align="stretch", flex=1)

    # --------------------------------------------------- detail / series / etc

    _meta_line = staticmethod(detail.meta_line)



    _fmt_ticks = staticmethod(detail.fmt_ticks)

    _action_btn = staticmethod(controls.action_btn)

    def _common_actions(self, item, server, prefix):
        return detail.common_actions(self._actions, self.tiles, item, server,
                                     prefix)

    def _download_btn(self, item, server, prefix):
        return detail.download_button(self._actions, self.tiles, item, server,
                                      prefix)

    def _remove_download(self, item):
        self._actions.remove_download(item)

    def _play_next_up(self, series_id, server):
        self._actions.play_next_up(series_id, server)

    def _shuffle_series(self, series_id, server):
        self._actions.shuffle_series(series_id, server)

    def _act_watched(self, item, server):
        self._actions.toggle_watched(item, server)

    def _act_favorite(self, item, server):
        self._actions.toggle_favorite(item, server)

    def _people_row(self, people, server):
        return detail.people_row(self.tiles, people)


    def _search(self, term):
        term = (term or "").strip()
        if not term:
            return
        self.navigate({"kind": "search", "server": self.server,
                       "term": term, "title": _("Search")})


    # ---------------------------------------- route loaders

    def _load_grid(self, route, ep):
        srv = route.get("server") or self.server
        parent = route["parent_id"]
        _n, sort_by, sort_order = SORTS[route.get("_sort", 0)]
        filters = route.get("_filters") or {}

        collections = bool(route.get("_collections"))

        def work():
            if collections:
                # Collections are server-wide and recursive (a BoxSet
                # can gather items from several libraries), so this is a
                # different query, not a filter on the library.
                items, total = self.source.get_movie_collections(
                    srv, sort_by=sort_by, sort_order=sort_order,
                    filters=filters)
            else:
                items, total = self.source.get_library_items(
                    srv, parent, sort_by=sort_by, sort_order=sort_order,
                    filters=filters)
            vals = route.get("_filtervals")
            if vals is None:
                try:
                    vals = self.source.get_filter_values(srv, parent)
                except Exception:
                    vals = {"genres": [], "years": []}
            return items, total, vals

        def done(res):
            items, total, vals = res
            route["_items"], route["_filtervals"] = items, vals
            # Random reshuffles server-side on every request, so page two is
            # drawn from a different ordering than page one: paging it yields
            # duplicates and silently skips items. Reporting the first page as
            # the whole list is what the Tk browser did, and _page_more's
            # "an empty page ends the list" rule can never fire here because a
            # reshuffle always returns something.
            route["_total"] = len(items) if sort_by == "Random" else total
            # The toggle only makes sense on a movies library, and only
            # when the source can answer it (the offline catalog can't).
            route["_collection_capable"] = (
                route.get("collection_type") == "movies"
                and hasattr(self.source, "get_movie_collections"))
        self._route_async(route, work, done, ep)

    def _load_person(self, route, ep):
        srv = route.get("server") or self.server
        # The repository has taken sort_by/sort_order since it was written;
        # this was the one caller that never passed them, so the dropdown
        # had nowhere to land.
        _label, sort_by, sort_order = SORTS[route.get("_sort", 0)]

        def work():
            return self.source.get_person_items(
                srv, route["person_id"],
                sort_by=sort_by, sort_order=sort_order)

        def done(res):
            items, total = res
            route["_items"] = items
            # Random reshuffles server-side per request, so paging it yields
            # duplicates and skips. Cap at the first page, as the grid does
            # and as Tk did — the cap lived only in _load_grid, so the
            # filmography had the corruption the cap exists to prevent.
            route["_total"] = len(items) if sort_by == "Random" else total
        self._route_async(route, work, done, ep)
