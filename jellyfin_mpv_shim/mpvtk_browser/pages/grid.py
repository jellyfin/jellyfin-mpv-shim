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
    Box, Column, Dropdown, Row, Spacer, Text, VScroll,
)
from .. import dialogs, pagination, theme, view_prefs
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

#: Sorts only some libraries offer, APPENDED to SORTS -- a route stores its
#: sort as an index into that list, so anything inserted would silently
#: re-point every route already carrying one.
#:
#: "Date Added" on a TV library is when the *series* was created, which for a
#: show you have followed for three years is three years ago -- so the
#: library's own "what is new" question has no answer in the base list.
#: DateLastContentAdded is the newest episode, and is what jellyfin-web
#: offers there under this label (SortButton.tsx: OptionDateEpisodeAdded).
#: The name is the server's: DateLastMediaAdded is not in its sort enum, and
#: an unknown sort is *ignored* rather than refused -- the grid silently
#: comes back in name order.
EXTRA_SORTS = {
    "tvshows": [(_("Date Episode Added"), "DateLastContentAdded",
                 "Descending")],
}
_LETTERS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def sorts_for(collection_type):
    """The sort menu for a library of this kind."""
    return SORTS + EXTRA_SORTS.get(collection_type or "", [])

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
NO_PLAY_ALL = frozenset({"tvshows", "books"})

#: Collection types with no library-wide play buttons AT ALL (neither Play
#: All nor Shuffle).
#:
#: Books, and only books. Half a books library cannot be played at all -- a
#: `Book` has no media source, so it is silently dropped from the queue --
#: and the other half is *audiobooks*, where a library-wide queue is every
#: chapter of every book in name order, started from the beginning. That
#: last part is what makes it worse than useless rather than merely odd:
#: playing a book from chapter one overwrites hours of position as it goes.
#: The audiobook's own folder has a real, resume-aware Play one click in.
#:
#: Shuffle is separately absurd here in a way it is not on a TV library --
#: a random episode is a reasonable ask, a random chapter of a random book
#: is not.
NO_LIBRARY_PLAY = frozenset({"books"})

#: What a by-name screen lists, per collection type. Mirrors
#: LibrarySource.GENRE_ITEM_TYPES; kept here too because the button
#: builds the spec and the page should not import the repository.
GENRE_ITEM_TYPES = {"movies": "Movie", "tvshows": "Series"}

log = logging.getLogger("mpvtk_browser.pages.grid")


def _image_type_of(view):
    """The artwork name to ask the server for, given a view-settings dict.

    ``None`` for "Auto", which asks for nothing beyond the browse defaults --
    the grid is shaped by whatever artwork comes back.
    """
    stored = (view or {}).get("imageType")
    named = view_prefs.shape_for(stored[0] if stored else None)
    return named[1] if named else None


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
        run = self.ctx.run
        invalidate = self.ctx.invalidate
        srv = route.get("server") or self.ctx.server
        parent = route["parent_id"]
        _n, sort_by, sort_order = self._sorts()[route.get("_sort", 0)]
        filters = route.get("_filters") or {}
        collections = bool(route.get("_collections"))
        ctype = route.get("collection_type")

        def work():
            # The view settings come FIRST, and not because render wants
            # them: they say which artwork this library is drawn with, and
            # that has to reach the item query as EnableImageTypes. Read
            # after the items and a library set to Banner asks for banners
            # it has already been answered without. It comes off a cached
            # blob (one document per server), so this is not a round trip
            # per library.
            # The route's own copy wins where it has one: a reload triggered
            # by changing the artwork setting runs while that save is still
            # in flight, and the server would answer with the value the user
            # just changed away from -- fetching the wrong tags and drawing
            # the grid back the way it was. Only a first load asks.
            view = route.get("_view")
            if view is None:
                get_view = getattr(source, "get_view_settings", None)
                view = (get_view(srv, parent, ctype)
                        if get_view else dict(_DEFAULT_VIEW))
            image_type = _image_type_of(view)
            if collections:
                # Collections are server-wide and recursive (a BoxSet
                # can gather items from several libraries), so this is a
                # different query, not a filter on the library.
                items, total = source.get_movie_collections(
                    srv, sort_by=sort_by, sort_order=sort_order,
                    filters=filters, image_type=image_type)
            else:
                items, total = source.get_library_items(
                    srv, parent, sort_by=sort_by, sort_order=sort_order,
                    filters=filters, image_type=image_type,
                    collection_type=ctype)
            # Paint the tiles BEFORE asking for the filter pickers. Nothing
            # on the first frame needs them and they are the slow half of
            # this load: Items/Filters scans the library server-side, 3.7s
            # against a real 950-series library here, all of it spent on a
            # spinner over items that had already arrived. jellyfin-web does
            # not fetch them with the items at all -- its filter dialog asks
            # when it is opened.
            #
            # Epoch-checked by hand and published mid-flight, exactly as the
            # home screen's two-stage load is: this write is outside the
            # run_async gate that protects the returned value, so without the
            # check, navigating away mid-load would repaint the grid the user
            # just left. One job rather than two, so nothing is submitted to
            # the pool from inside a pool worker.
            if run.epoch == epoch:
                self._install(items, total, view, sort_by)
                invalidate()
            vals = route.get("_filtervals")
            if vals is None:
                try:
                    vals = source.get_filter_values(srv, parent,
                                                    collection_type=ctype)
                except Exception:
                    log.debug("filter values unavailable", exc_info=True)
                    vals = {"genres": [], "years": []}
            return items, total, vals, view

        def done(res):
            items, total, vals, view = res
            route["_filtervals"] = vals
            self._install(items, total, view, sort_by)

        self.route_async(work, done, epoch)

    def _install(self, items, total, view, sort_by):
        """Publish a loaded page onto the route. Called twice -- once when
        the items land and once when the whole job does -- so it must stay
        idempotent."""
        route = self.route
        # Only where the route has none, which is the same rule ``load``
        # applies when it decides whether to ask the server at all. The two
        # calls are seconds apart -- the filter pickers are the slow half --
        # and the grid is on screen and its View settings reachable for all
        # of it, so re-publishing what the worker read would throw away a
        # change the user has made in the meantime. It is thrown away
        # *silently*: the save has already gone to the server, so the screen
        # would be left disagreeing with what is stored.
        if route.get("_view") is None:
            route["_view"] = view
        # Random reshuffles server-side on every request, so page two is
        # drawn from a different ordering than page one: paging it yields
        # duplicates and silently skips items. Reporting the first page as
        # the whole list is what the Tk browser did, and the paginator's
        # "an empty page ends the list" rule can never fire here because a
        # reshuffle always returns something. It is also what keeps a random
        # grid out of the windowed path below: total == what is loaded, so
        # there are no holes to ask about.
        total = len(items) if sort_by == "Random" else total
        route["_total"] = total
        # The list is `total` slots wide from here on, holes and all, so the
        # grid is its full height and the scrollbar its full length before
        # anything past the first page exists (#617). Spread over whatever is
        # already there rather than replacing it: this runs twice per load
        # (see above) and a window may have landed in between.
        route["_items"] = pagination.spread(
            route.get("_items") or [], total, items, 0)
        # The toggle only makes sense on a movies library, and only
        # when the source can answer it (the offline catalog can't).
        route["_collection_capable"] = (
            route.get("collection_type") == "movies"
            and hasattr(self.ctx.source, "get_movie_collections"))

    # -- render ------------------------------------------------------------

    def render(self, size):
        art = self.ctx.art
        tiles = art.tiles
        route = self.route
        items = route.get("_items")
        if items is None:
            # The SHELL stays and only the tiles blank -- **[iw]**: "what
            # should happen is the tiles blank out but the shell stays".
            #
            # This used to be `chrome.busy()` for the whole page, so a
            # filter tick, a sort change or a letter press replaced the
            # title, the filter bar and the A-Z rail with a spinner and
            # the library looked dead -- behind the filter panel, which
            # covers the middle of the window, the page simply emptied.
            #
            # The tiles genuinely are unknown for the length of the query
            # and drawing the previous ones would be a small lie; the
            # title, the controls and their state are not unknown, and
            # blanking them was the whole of the problem. Same Column,
            # pad and gap as the loaded path, so nothing above the tiles
            # moves when they arrive.
            return Column(self._header(None, size[0]) + [chrome.busy()],
                          pad=chrome.CONTENT_PAD, gap=GRID_GAP,
                          flex=1, align="stretch")
        header = self._header(items, size[0])
        if view_prefs.is_list(self._view("imageType"), self._view("viewType")):
            return self._list_view(items, header)
        geom, image_type = self._grid_shape(items)
        labels = (bool(self._view("showTitle")),
                  bool(self._view("showYear")))
        if not labels[0]:
            # The strip reserves caption_h under every tile whether or not
            # anything is drawn there, so the reservation has to follow what
            # will actually be drawn -- too much is a band of background,
            # too little puts a caption over the next row.
            #
            # Titles off, years on is ONE line (the year moves up into the
            # title's place; see strips._paint_caption), so drop exactly the
            # title line: its size plus the gap under it. Derived from the
            # geometry rather than a constant, because a theme's cover size
            # scales all of these together.
            geom = dataclasses.replace(
                geom,
                caption_h=(max(0, geom.caption_h - geom.title_size - 7)
                           if labels[1] else 0))
        if self._pages.enabled():
            return self._paged_grid(size, header, geom, image_type, labels)
        # Ask for the rows about to be drawn, from the SAME window the
        # renderer composites -- see TileRenderer.row_window.
        cols = tiles.cols(size[0], geom)
        first, last = tiles.row_window(size, geom, "grid", self.HEAD_H)
        self._window(first * cols, (last + 1) * cols)
        rows = header + tiles.grid_of(
            items, "grid", size, geom=geom, image_type=image_type,
            scroll_id="grid", head_h=self.HEAD_H, labels=labels)
        return VScroll(
            Column(rows, pad=chrome.CONTENT_PAD, gap=GRID_GAP,
                   align="stretch"), id="grid",
            flex=1,
            # Back-nav lands where you left it (see Page.parked_scroll).
            offset=self.parked_scroll("grid"),
            # Declare the row pitch, so the renderer can align to it when
            # it needs to -- people scroll libraries fast, and where a frame
            # is dear (every visible row repositioned, a full 4K recomposite
            # each frame) quantizing turns per-frame smear into stable,
            # row-aligned frames. Not unconditional: see widgets.Scroll.
            snap=geom.strip_h + GRID_GAP,
            # Exact content-y of the first tile row (not the approximate
            # HEAD_H): a snap stop landing a few px short leaves the previous
            # row's caption — its year label — peeking at the top edge.
            snap_off=tiles.header_offset(header),
            # No page-on-approach callback: the window is asked for at
            # render time now, and a scroll that moves far enough to change
            # it is already a repaint (ScrollState.on_scroll). What the
            # callback does is let a window that FAILED be asked for again --
            # render cannot retry on its own without becoming a request per
            # frame. See Paginator.rewindow.
            on_scroll=lambda off, mx: art.scroll.on_scroll(
                "grid", off, mx, lambda o, m: self._pages.rewindow(route)),
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
            Text(route.get("title", ""), size="page", bold=True),
            Spacer(flex=1),
            controls.action_btn("settings", _("View"), "grid-viewcfg",
                                self._open_view_settings, size=16),
        ], align="center", w=max(0, (width or 0) - 2 * chrome.CONTENT_PAD))]
        header.append(self._filter_bar(width))
        # The count line is redundant with the pagination bar's "of N".
        #
        # The total alone, not "N of M": the list is M entries long from the
        # first frame now, so "how many are loaded" is an implementation
        # detail that used to be visible only because the grid could not
        # show what it had not fetched.
        if not self._pages.enabled():
            header.append(Text(_("%d items") % (route.get("_total") or 0),
                               size="caption", color=theme.SUBTLE_FG))
        return header

    def _view_controls(self):
        """Buttons that LEAVE this library -- Genres, Networks.

        ...and **Collections**, which is one of these and not a filter.
        It sets `route["_collections"]`, tears down `_items`, `_total`,
        the grid shape and the paginator, and comes back with a different
        item type wearing a different tile shape -- its own docstring
        says "a different query rather than a filter". The test is
        whether it composes: everything in `_filters` intersects, and
        Collections cannot intersect with anything. **[iw]**: "we should
        honestly treat collections as a door."

        (This docstring used to argue the opposite -- that Collections
        "really is a filter" -- which is why it sat among the checkboxes.)

        They lead the filter row rather than sitting among the filters,
        because they are a different kind of thing: a filter changes what
        this grid shows and these navigate somewhere else entirely.

        jellyfin-web reaches both through library tabs, which we do not
        have -- so leading the bar is the nearest thing to a tab strip.
        """
        route = self.route
        out = []
        if route.get("collection_type") in STUDIO_LIBRARIES:
            # Web calls this "Networks" on a TV library and offers it only
            # there, which is where studio metadata actually is.
            out.append(controls.action_btn("apartment", _("Networks"),
                                           "grid-studios", self._open_studios))
        if route.get("collection_type") in GENRE_LIBRARIES:
            out.append(controls.action_btn("label", _("Genres"),
                                           "grid-genres", self._open_genres))
        if route.get("_collection_capable"):
            # ``on``, not chrome_button_style: that helper is empty for any
            # theme that did not ask for accented chrome -- which is the
            # stock one -- so the only toggle on this bar had NO visible
            # active state on the default theme. Same fill every other
            # toggle in the app uses (Watched, Favorite), and the accent is
            # the whole signal that this grid is showing something else.
            out.append(controls.action_btn(
                "video_library", _("Collections"), "grid-collections",
                self._toggle_collections, on=bool(route.get("_collections"))))
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
        active = self._active_filters()
        bar = Row([
            Dropdown("grid-sort", [s[0] for s in self._sorts()],
                     selected=route.get("_sort", 0), w=180,
                     on_select=lambda i, v: self._set("_sort", i)),
            # One button instead of the three drop-downs and two
            # checkboxes that used to live here. The bar had 277px spare
            # at 1280 -- one control's width, with genre names that size
            # to their contents -- so the eight categories web offers
            # could not have gone on it at any window size.
            #
            # The count is the badge: web draws a dot, but a dot says
            # "something is on" and a number says how much of what you
            # are looking at has been hidden, which is the question you
            # ask when a library looks short.
        ] + ([
            controls.action_btn(
                "filter_alt",
                _("Filter (%d)") % active if active else _("Filter"),
                "grid-filter", self._open_filters,
                primary=bool(active)),
        ] if self._filters_offered() else []) + ([
            # Inline, not pushed to the far edge. They sat there because
            # the bar used to be five filter controls wide and they had to
            # go somewhere; with one Filter button there is nothing to be
            # on the other side OF. **[iw]**: "we should put shuffle next
            # to Filter. It doesn't make sense to put it on the other side
            # of the UI anymore."
            #
            # action_btn, not Button, and that is the rule its own
            # docstring states: a plain Button resolves its label from the
            # type scale and comes out taller than the icon buttons beside
            # it. Moving these inline is what made it visible -- across the
            # bar from Filter, nothing was next to them to be uneven with.
            controls.action_btn("play_arrow", _("Play All"), "grid-playall",
                                self._play_all),
        ] if self._play_all_capable() else []) + ([
            controls.action_btn("shuffle", _("Shuffle"), "grid-shuffle",
                                self._shuffle),
        ] if self._shuffle_capable() else []) + [Spacer(flex=1)],
            gap=10, align="center")
        bar = self._fit_bar(bar, self._view_controls(), width)
        cur_letter = filters.get("letter")
        cells = [
            # flex + align="center" centres the glyph horizontally; a bare
            # Text is packed at the box's left edge (Box only centres on its
            # cross axis), which left every letter hugging its left border.
            Box([Text(ch, size="small", align="center", flex=1,
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
            for ch in _LETTERS]
        # 27 cells at 28px is 754px of row, which is wider than the page
        # itself once the UI scale is up -- a 1280px window at 200% is a
        # 640px page. Wrapped rather than shrunk: the cell is already only
        # 26px, and the point of an A-Z bar is that every letter is one
        # click away, which a horizontal scroll would take back.
        letters = chrome.wrap_row(cells, width - 2 * chrome.CONTENT_PAD,
                                  gap=2, row_gap=4)
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
            body = [Text(_("Loading…"), size="large", color=theme.SUBTLE_FG)]
        else:
            body = tiles.grid_of(page_items, "grid", size, geom=geom,
                                 image_type=image_type, labels=labels)
        return Column(header + body, pad=chrome.CONTENT_PAD, gap=GRID_GAP,
                      align="stretch", flex=1)

    # -- data ---------------------------------------------------------------

    def _sorts(self):
        """This route's sort menu. A TV library has one the others do not --
        see EXTRA_SORTS -- and a route's stored ``_sort`` is an index into
        whichever list its own screen offers."""
        return sorts_for(self.route.get("collection_type"))

    def _bound_query(self):
        """``(sort_by, sort_order, filters, person, srv, image_type,
        collections)`` read NOW, on the loop thread. The sort/filters a page
        is fetched with must be the ones it was asked for, not whatever they
        are when it lands -- and the artwork it asks the server for must be
        the one the grid is being drawn with, or page two of a Banner view
        arrives with no banners.

        ``collections`` for the same reason as the rest, and it was the one
        thing missing: `_fetch_at` read it live off the route, so a page-in
        submitted before the Collections toggle and landing after it would
        answer from whichever endpoint the flag named by then -- the movies
        query spliced into a list of collections or the reverse. Not
        reachable today, because `_toggle_collections` drops `_items` and
        the loading shell is drawn before any window is computed; bound
        here so that stays a property of this tuple rather than of what
        render happens to do.
        """
        _n, sort_by, sort_order = self._sorts()[self.route.get("_sort", 0)]
        return (sort_by, sort_order,
                self.route.get("_filters") or {},
                self.route.get("person_id"),
                self.route.get("server") or self.ctx.server,
                _image_type_of(self.route.get("_view")),
                bool(self.route.get("_collections")))

    def _fetch_at(self, start, limit=None, bound=None):
        """One page of results. ``bound`` is a _bound_query() tuple captured
        on the loop thread; omitted only where the caller is already on it."""
        (sort_by, sort_order, filters, person, srv,
         image_type, collections) = bound or self._bound_query()
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
        if collections:
            return source.get_movie_collections(
                srv, start_index=start, sort_by=sort_by,
                sort_order=sort_order, filters=filters,
                image_type=image_type, **kw)
        return source.get_library_items(
            srv, self.route["parent_id"], start_index=start,
            sort_by=sort_by, sort_order=sort_order, filters=filters,
            image_type=image_type, **kw)

    def _window(self, first, last):
        """Fetch the items in ``[first, last)`` that are not loaded yet.

        Called from render, because which items are visible is a question
        about geometry. A Random sort is exempt: it reshuffles per request,
        so ``_install`` reports what it loaded as the whole list and there
        are no holes to fill.

        The query is bound HERE, on the loop thread, not inside the worker:
        the sort and filters a page is fetched with must be the ones it was
        asked for, not whatever they are when it lands.
        """
        bound = self._bound_query()
        if bound[0] == "Random":
            return

        def put(r, items, total):
            r["_items"], r["_total"] = items, total

        self._pages.window(
            self.route, first, last,
            lambda r: (r.get("_items") or [], r.get("_total") or 0),
            put,
            lambda start, limit: self._fetch_at(start, limit, bound=bound))

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
        # A hole draws as an empty row rather than being skipped: the row
        # index IS the item's position, which is what keeps the table its
        # full height and the scrollbar honest while the windows load.
        rows = [
            {"cells": [it.get("Name") or "",
                       str(it.get("ProductionYear") or "")
                       if self._view("showYear") else "",
                       _fmt_runtime(it.get("RunTimeTicks"))],
             "item": it}
            if it else {"cells": ["", "", ""], "item": None}
            for it in items]
        head_h = self.ctx.art.tiles.header_offset(header)
        table = self.ctx.art.tiles.item_list(
            rows, "grid", on_click=lambda i: self.ctx.art.tiles.on_open(
                items[i]),
            scroll_id="grid",
            # What sits above the table inside the same Column, so the
            # window is computed against the table's own rows rather than
            # against the page. header_offset knows this Column's pad and
            # gap because it is the one the grid uses.
            head_h=head_h)
        # The same window the Table materializes, so the rows fetched are
        # the rows drawn.
        self._window(*self.ctx.art.tiles.list_window(
            len(items), "grid", head_h))
        return VScroll(
            Column(header + [table], pad=chrome.CONTENT_PAD, gap=GRID_GAP,
                   align="stretch"), id="grid", flex=1,
            offset=self.parked_scroll("grid"),
            on_scroll=lambda off, mx: self.ctx.art.scroll.on_scroll(
                "grid", off, mx,
                lambda o, m: self._pages.rewindow(self.route)))

    def _sort_bar(self):
        """Just the sort dropdown, for routes with no filterable axes.

        On ``GridPage`` rather than a subclass because two of them want it
        now. The node id carries the route kind: the renderer targets by id
        and keeps per-node state against it, so two screens sharing one id
        would share a dropdown -- and the id was literally "person-sort"
        while the list route was being built on top of it.
        """
        return Row([
            Text(_("Sort"), size="small", color=theme.SUBTLE_FG),
            Dropdown("%s-sort" % self.kind, [s[0] for s in self._sorts()],
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
        frame and rolls back if the server refuses.
        """
        route = self.route
        if setting == "imageType" and value != view_prefs.LIST_IMAGE_TYPE:
            self._retire_legacy_list_view()
        view = dict(route.get("_view") or _DEFAULT_VIEW)
        previous, key = view.get(setting) or (None, None)
        if value == previous:
            return
        was = _image_type_of(route.get("_view"))
        view[setting] = (value, key)
        route["_view"] = view
        # The parked median is only consulted when there is no override, but
        # it was computed for a different shape; drop it so returning to
        # "Primary" measures afresh.
        # The parked median was measured for a different shape.
        route.pop("_grid_shape", None)
        self._redraw_or_refetch(was)
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
            was_rolled = _image_type_of(route.get("_view"))
            rolled = dict(route.get("_view") or {})
            rolled[setting] = (previous, key)
            route["_view"] = rolled
            route.pop("_grid_shape", None)
            self.ctx.status(_("That view setting could not be saved."))
            self._redraw_or_refetch(was_rolled)

        self.ctx.run.run(work, lambda _r: None, self.ctx.run.epoch,
                         on_error=failed)

    def _retire_legacy_list_view(self):
        """Clear a ``viewType`` an earlier build of this client wrote.

        ``view_prefs.is_list`` still honours that key -- a library put in
        list view before the setting moved onto web's shared ``imageType``
        has to keep coming up as a list -- which means nothing that writes
        only ``imageType`` can take such a library back OUT of the list. The
        checkbox saved the grid value, the legacy key went on outvoting it,
        and it came back ticked on the next frame: a control that read as
        dead.

        Called from the top of :meth:`_set_view`, ahead of *both* the no-op
        check and the snapshot it takes of ``_view``. Ahead of the check
        because the box unticks by writing the imageType the library already
        had, so the write it rides on is very often no write at all; ahead of
        the snapshot because this sets ``_view`` itself, and a caller holding
        an older copy puts the legacy value straight back.

        One-way, and deliberately so. Nothing writes ``viewType`` any more,
        so this is a migration rather than a setting: once the shared key has
        been asked the question, it is the only one that answers it.
        """
        if view_prefs.is_list_view(self._view("viewType")):
            self._set_view("viewType", view_prefs.GRID_VIEW)

    def _redraw_or_refetch(self, was):
        """Repaint after a view change -- and re-ask the server when the
        change was one it needs to hear about.

        Which artwork the tiles are drawn with is part of the QUERY: the
        server only sends the image tags a request names, so a grid fetched
        as Auto has no Banner in its ImageTags and switching to Banner can
        only fall back to the poster it already has. Redrawing was the whole
        of this, and it is why the setting looked like it did nothing.

        In place rather than through ``_reload``: the items are not stale,
        only the tags on them, so the grid keeps what it is showing instead
        of blinking a spinner over it. The reload re-reads the view settings,
        and now prefers the route's own copy (see :meth:`load`) -- the save
        is still in flight, so the server would answer with the old value.

        The test is what the QUERY would be, not which setting was picked:
        Auto and Poster ask for exactly the same artwork and differ only in
        the shape it is drawn at, so switching between them is a repaint.

        ``nav.load`` rather than ``nav.reload`` for a route that is no longer
        on screen. The rollback path reaches here from ``on_error``, which is
        deliberately NOT epoch-gated (see AsyncRunner), so a save that fails
        after the user has walked away lands on the route they left --
        and ``reload`` bumps the epoch, which would cancel the in-flight load
        of whatever IS on screen and strand it on a spinner with nothing left
        to re-issue it. ``load`` re-runs this route's own loader without
        touching the epoch or repainting, which is exactly what it is for.
        """
        from ..repository import browse_image_types

        now = _image_type_of(self.route.get("_view"))
        if browse_image_types(was) == browse_image_types(now):
            self.ctx.invalidate()
            return
        # Drop what is loaded before re-asking. _install SPLICES its page in
        # now (windowing, #617) rather than replacing the list, so without
        # this the refetch would refresh items 0..99 and leave every later
        # window holding the tags fetched under the OLD EnableImageTypes --
        # permanently, because a filled slot is never re-requested. That is
        # exactly the bug this method exists to fix, reintroduced past the
        # first page.
        for k in ("_items", "_total", "_win_tried", "_win_load"):
            self.route.pop(k, None)
        # ...and the page cache, which holds tiles carrying the artwork tags
        # this refetch exists to replace. Its page size is unchanged, so
        # `ensure` would serve them straight back and the setting would look
        # like it had done nothing -- which is the bug this method is named
        # for, one level down.
        self._pages.reset(self.route)
        if self.ctx.nav.is_current(self.route):
            self.ctx.nav.reload(self.route)
        else:
            self.ctx.nav.load(self.route)

    #: Filter keys that are NOT drawn by the panel. `letter` is the A-Z
    #: rail down the side of the grid, which is a control of its own.
    _NOT_IN_PANEL = ("letter",)

    def _panel_keys(self):
        """Filter keys this source can actually apply.

        Asked of the SOURCE rather than tested as "are we offline": a
        page is not supposed to care which source it has, and the honest
        question is what the source can do. Empty on a downloaded
        library, which is what takes the Filter button off the bar.

        `getattr` with a default because the shell's stand-ins and any
        third source predate the capability; the default is the online
        set, so a source that says nothing keeps today's behaviour rather
        than silently losing its filters.
        """
        from ..repository import SUPPORTED_FILTERS
        return getattr(self.ctx.source, "supported_filters",
                       SUPPORTED_FILTERS)

    def _filters_offered(self):
        """Whether to draw the Filter button at all."""
        return bool(set(self._panel_keys()) - set(self._NOT_IN_PANEL))

    def _active_filters(self):
        """How many filters are on. Drives the button's dot.

        Counts only what this source can APPLY -- a key it ignores is not
        a filter that is on, and counting it made the badge report
        filtering that was not happening.
        """
        f = self.route.get("_filters") or {}
        keys = self._panel_keys()
        return sum(1 for k, v in f.items()
                   if v and k not in self._NOT_IN_PANEL and k in keys)

    def _open_filters(self):
        """The filter panel: a modal, because it is a page of controls."""
        # Getters, not values. `route.get("_filters") or {}` answers with
        # a FRESH dict when nothing is set yet, and `_toggle_filter` then
        # writes to the one `setdefault` makes -- a different object -- so
        # every tick was drawn from a snapshot taken when the panel opened
        # and nothing ever moved. `_clear_filters` replaces the dict
        # outright, which breaks it a second way.
        #
        # This is the standing browser footgun (CLAUDE.md): a widget tree
        # is a snapshot, so read mutable state INSIDE the builder.
        self.ctx.dialogs.filter_panel(
            lambda: self.route.get("_filtervals") or {},
            lambda: self.route.get("_filters") or {},
            self._set_filter, self._panel_toggle, self._clear_filters,
            collection_type=self.route.get("collection_type"))

    def _panel_toggle(self, key):
        """A panel checkbox. Repaints the dialog as well as reloading.

        The renderer flips a Dropdown's own selection optimistically, but
        **a Checkbox is drawn from its `checked` argument** and only a
        redraw can move the tick -- the standing footgun in this codebase
        (CLAUDE.md), and the reason the Add to Playlist dialog once had a
        Private box that flipped invisibly.
        """
        self._toggle_filter(key)
        self.ctx.invalidate()

    def _clear_filters(self):
        """Everything off, except the A-Z rail, which is not in here."""
        keep = {k: v for k, v in (self.route.get("_filters") or {}).items()
                if k in self._NOT_IN_PANEL}
        self.route["_filters"] = keep
        self._reload()
        self.ctx.invalidate()

    def _fit_bar(self, bar, extras, width):
        """``bar`` with ``extras`` appended, or stacked under it if that
        would not fit.

        Measured rather than switched on a width constant, for the same
        reason the top bar is: what fits depends on how many controls this
        particular library has -- a movies library carries a Collections
        toggle and a Genres button that a music one does not, and the sort
        and genre dropdowns are sized to their contents.
        """
        avail0 = (width or 0) - 2 * chrome.CONTENT_PAD
        if not extras:
            # Still width it: the trailing Spacer needs leftover to absorb
            # whether or not this library has doors on the bar, and this
            # is the path a movies library takes.
            return (Row(bar.children, gap=10, align="center", w=avail0)
                    if avail0 > 0 else bar)
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
            # An explicit width, so the trailing Spacer has leftover to
            # absorb and the play buttons sit at the right edge. A Row
            # sizes to what it measures otherwise, which parked them
            # immediately after the Filter button.
            return Row(extras + bar.children, gap=10, align="center",
                       w=avail)
        # Above, not below: these are the doors out of this library, and a
        # row of them under the filters reads as more filtering.
        return Column([Row(extras, gap=10, align="center"),
                       Row(bar.children, gap=10, align="center", w=avail)],
                      gap=8, align="stretch")

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
                # `if i` for the same reason auto_geom has it five lines
                # up: `items` is the sparse list, and a hole has no .get.
                loaded = [i for i in items or () if i]
                ratios = sorted(r for r in
                                (i.get("PrimaryImageAspectRatio")
                                 for i in loaded)
                                if isinstance(r, (int, float)) and r > 0)
                log.debug(
                    "grid %s (%s): %d/%d items carry a ratio %s -> %dx%d %s",
                    route.get("title"), route.get("collection_type"),
                    len(ratios), len(loaded),
                    ("%.3f..%.3f" % (ratios[0], ratios[-1])) if ratios
                    else "(none)",
                    parked[0].tile_w, parked[0].tile_h, parked[1])
        return parked

    # -- header actions -----------------------------------------------------

    def _reload(self):
        """Re-run this route's query, **keeping what is on screen until the
        new results land**.

        This used to pop ``_items``, and ``render`` answers a missing
        ``_items`` with ``chrome.busy()`` for the whole page -- title,
        filter bar, A-Z rail and all. So every filter tick, every sort
        change and every letter press blanked the library and drew a
        spinner over it. Behind the filter panel, which covers the middle
        of the window, all that was visible of that was the page going
        empty: **[iw]** "it makes the page look dead behind a modal while
        re-querying".

        Stale-while-revalidate, the rule refresh_live_tv already argues
        for: a re-read of a screen the user is looking at must not blink a
        spinner over what they are reading. Unlike that one this DOES
        bump the epoch -- the query has changed, so anything in flight is
        answering the wrong question -- and unlike that one the old data
        is genuinely wrong rather than merely stale. It stays up because
        an empty page for the length of a query says nothing true either,
        and it says it much louder.

        ``_total`` is deliberately NOT dropped. It is the header's item
        count, which is shell rather than content -- and zeroing it puts
        "0 items" over the spinner, which is a worse thing to say than a
        count one second out of date. ``_install`` overwrites it.
        """
        route = self.route
        for k in ("_items", "_grid_shape", "_win_tried", "_win_load"):
            route.pop(k, None)
        route["_loading"] = False
        self._pages.reset(route)
        self.ctx.nav.reload(route)

    def _set(self, key, value):
        self.route[key] = value
        self._reload()

    def _set_filter(self, key, value):
        self.route.setdefault("_filters", {})[key] = value
        self._reload()

    def _toggle_filter(self, key):
        f = self.route.setdefault("_filters", {})
        on = not f.get(key)
        f[key] = on
        if on:
            # Turning one on turns its opposite off. Two of these pairs
            # cannot be sent at all rather than merely being pointless:
            # Played+Unplayed share one comma-joined `Filters` parameter
            # and the server answers that with HTTP 400, and SD+HD share
            # one tri-state `IsHd`. See dialogs.MUTUALLY_EXCLUSIVE.
            #
            # `if on` is the correct spelling and is not observable:
            # clearing the partner while switching a box OFF could only
            # matter if the partner were on, and this is the one thing
            # that lets it be. It is written this way because it says what
            # it means, not because a test can tell -- a test that pinned
            # it would be asserting nothing.
            other = dialogs.MUTUALLY_EXCLUSIVE.get(key)
            if other:
                f[other] = False
        self._reload()

    def _toggle_collections(self):
        """Movies library <-> its collections, like jellyfin-web's toggle.
        Collections are server-wide and recursive, so this is a different
        query rather than a filter."""
        self.route["_collections"] = not self.route.get("_collections")
        for k in ("_items", "_total", "_loading", "_grid_shape",
                  "_win_tried", "_win_load"):
            self.route.pop(k, None)
        # The paginator too. `ensure` only rebuilds its cache when the page
        # SIZE changes, and this does not change it -- so a paginated grid
        # went on drawing the films it had cached for pages n-1..n+1 over a
        # list that is now this library's collections, and paging around the
        # neighbourhood did not heal it. Everything else that replaces the
        # result set goes through _reload, which does this.
        self._pages.reset(self.route)
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
        return (self.route.get("collection_type")
                not in (NO_PLAY_ALL | NO_LIBRARY_PLAY))

    def _shuffle_capable(self):
        """Whether this library gets a Shuffle button. See NO_LIBRARY_PLAY."""
        return self.route.get("collection_type") not in NO_LIBRARY_PLAY

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
        ctype = self.route.get("collection_type")

        def work(source, srv, parent, bound):
            sort_by, sort_order, filters = bound[0], bound[1], bound[2]
            # The collection type too: it is part of the grid's query now,
            # and this button's whole contract is that it asks the same one.
            return source.get_play_all_ids(srv, parent, sort_by=sort_by,
                                           sort_order=sort_order,
                                           filters=filters,
                                           collection_type=ctype)

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
            # Same Random cap as the grid: the server reshuffles per request,
            # so paging one yields duplicates and skips.
            total = len(items) if sort_by == "Random" else total
            route["_total"] = total
            # ...and the same padding. This route inherits GridPage.render,
            # so it IS windowed -- without the padding the list stayed one
            # page long while _total said otherwise, and the scroller jumped
            # the moment a window landed, which is #617's symptom itself.
            route["_items"] = pagination.spread([], total, items, 0)

        self.route_async(work, done, epoch)

    def _sort_args(self):
        if not self._sortable():
            return None, None
        _label, sort_by, sort_order = self._sorts()[
            self.route.get("_sort", 0)]
        return sort_by, sort_order

    def _bound_query(self):
        # No image type: a list route's shape comes from its spec, not from
        # a library's view settings (see SHAPES below). No collections
        # either -- that toggle belongs to a library grid -- but the arity
        # is the base class's, because `_window` and `_page_fetcher` are
        # inherited and unpack what this returns.
        sort_by, sort_order = self._sort_args()
        return (sort_by, sort_order, self.route.get("_filters") or {}, None,
                self.route.get("server") or self.ctx.server, None, False)

    def _fetch_at(self, start, limit=None, bound=None):
        sort_by, sort_order, filters, _person, srv, _itype, _coll = (
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
        header = [Text(self.route.get("title", ""), size="page", bold=True)]
        if self._sortable():
            header.append(self._sort_bar())
        if not self._pages.enabled():
            header.append(
                Text(_("%d items") % (self.route.get("_total") or 0),
                     size="caption", color=theme.SUBTLE_FG))
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
        _label, sort_by, sort_order = self._sorts()[route.get("_sort", 0)]

        def work():
            return source.get_person_items(
                srv, route["person_id"],
                sort_by=sort_by, sort_order=sort_order)

        def done(res):
            items, total = res
            # Random reshuffles server-side per request, so paging it yields
            # duplicates and skips. Cap at the first page, as the grid does
            # and as Tk did — the cap lived only in the grid loader, so the
            # filmography had the corruption the cap exists to prevent.
            total = len(items) if sort_by == "Random" else total
            route["_total"] = total
            # Padded for the same reason ListPage is: this route is windowed
            # by the inherited render, so an unpadded list resizes the
            # scroller under the thumb as each window lands.
            route["_items"] = pagination.spread([], total, items, 0)

        self.route_async(work, done, epoch)

    def _header(self, items, width=0):
        return [Text(self.route.get("title", ""), size="page", bold=True),
                self._sort_bar()]

