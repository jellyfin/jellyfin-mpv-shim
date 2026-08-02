"""The music library (tabbed) and the shared base for the music screens.

``MusicPage`` holds the two things album, artist and genre all draw — the
Play / Shuffle / Queue / Instant Mix bar, and the title/metadata/overview
block. They were methods on the browser because five routes shared them and
a mixin was the only place two pages could both reach; a base class is what
"these four screens are the same kind of screen" actually looks like.
"""

from ...i18n import _
from ...mpvtk.widgets import (
    Checkbox, Column, Row, Spacer, Text, VScroll)
from .. import pagination, theme
from ..components import chrome, controls, detail as detail_components
from ..tile_renderer import GRID_GAP
from .base import Page


class MusicPage(Page):
    """Shared furniture for the music screens. Not a route on its own."""

    def action_bar(self, server, ids, seed_id, prefix="ma", items=None):
        """Play / Shuffle / Queue / Instant Mix for a set of track ids.

        The first three are dropped when there are no ids: the artist page
        renders this bar even when the song fetch failed, and play_list
        returns silently on an empty list, so they were dead clicks. Instant
        Mix stays — it seeds from the container, not the tracks."""
        actions = self.ctx.actions
        btns = []
        if ids:
            btns += [
                controls.action_btn("play_arrow", _("Play"), prefix + "-play",
                                    lambda: actions.play_list(ids, server, 0,
                                                              audio=True,
                                                              items=items),
                                    primary=True),
                controls.action_btn("shuffle", _("Shuffle"),
                                    prefix + "-shuffle",
                                    lambda: actions.play_shuffle(ids, server)),
                controls.action_btn("playlist_add", _("Add to Queue"),
                                    prefix + "-queue",
                                    lambda: actions.queue_items(ids, server)),
            ]
        if seed_id:
            btns.append(controls.action_btn(
                "queue_music", _("Instant Mix"), prefix + "-mix",
                lambda: actions.instant_mix(seed_id, server)))
        return Row(btns, gap=8, align="center")

    def header_text(self, item, tracks, width):
        """Title / metadata / overview for an album or artist page.

        Both were a bare title: no cover, no year or genre, no Overview, and
        on the artist page no heading over the album grid either. Tk showed
        all of it, and on a music library it is most of what tells one entry
        from another."""
        out = [Text(item.get("Name") or self.route.get("title", ""), size=28,
                    bold=True)]
        meta = [x for x in (detail_components.meta_line(item),
                            (_("%d tracks") % len(tracks)) if tracks else "")
                if x]
        if meta:
            out.append(Text("   ·   ".join(meta), size=15,
                            color=theme.SUBTLE_FG))
        overview = (item.get("Overview") or "").strip()
        if overview:
            out.append(chrome.paragraph(
                overview, 15, self.ctx.art.tiles.body_w(width)))
        return out


class MusicLibraryPage(MusicPage):
    """The tabbed music library: albums / album artists / artists / songs /
    genres."""

    kind = "music"

    TABS = (("albums", _("Albums")),
            ("albumartists", _("Album Artists")),
            ("artists", _("Artists")),
            ("songs", _("Songs")),
            ("genres", _("Genres")))

    #: Tabs whose contents are a list or a single unpaged request rather than
    #: a server-paged grid, so they scroll instead of paginating.
    UNPAGED = ("songs", "genres")

    # -- load --------------------------------------------------------------

    def load(self, epoch):
        route = self.route

        def done(res):
            items, total = res
            route["_total"] = total
            # `total` slots from the first frame, most of them holes (#617).
            # The genres tab reports its own length, so it pads to exactly
            # what it loaded and windows to nothing -- the same shape that
            # exempts a Random library grid.
            route["_data"] = pagination.spread([], total, items, 0)
            route["_loading"] = False

        fetch = self._fetch_for_tab()      # bind the tab here, not later
        self.route_async(lambda: fetch(0), done, epoch)

    def _window(self, first, last):
        """Fetch the tiles in ``[first, last)`` that are not loaded yet.

        Called from render, like GridPage's: which items are visible is a
        question about geometry. The tab is bound HERE, on the loop thread,
        for the reason _fetch_for_tab's docstring gives -- reading it when
        the page lands resolves against whichever tab the user switched to.
        """
        fetch = self._fetch_for_tab()

        def put(r, items, total):
            r["_data"], r["_total"] = items, total

        self.ctx.art.pages.window(
            self.route, first, last,
            lambda r: (r.get("_data") or [], r.get("_total") or 0),
            put, lambda start, limit: fetch(start, limit))

    def _fetch_for_tab(self, limit=None):
        """``fetch(start)`` (or ``fetch(start, limit)``) for the current tab.

        **Call this on the loop thread.** It binds the server, library and
        tab *now*; reading them when the page lands would resolve against
        whichever tab the user had switched to by then.
        """
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server
        parent = route["parent_id"]
        tab = route.get("_tab", "albums")

        def fetch(start_index, lim=None):
            kw = {} if lim is None else {"limit": lim}
            if tab == "albumartists":
                return source.get_album_artists(srv, parent,
                                                start_index=start_index, **kw)
            if tab == "artists":
                return source.get_artists(srv, parent,
                                          start_index=start_index, **kw)
            if tab == "songs":
                return source.get_songs(srv, parent,
                                        start_index=start_index, **kw)
            if tab == "genres":
                # Genres are unpaged server-side, so they report their own
                # length as the total.
                genres = source.get_music_genres(srv, parent)
                return (genres if start_index == 0 else []), len(genres)
            return source.get_music_albums(srv, parent,
                                           start_index=start_index, **kw)
        return fetch

    # -- render ------------------------------------------------------------

    def render(self, size):
        art = self.ctx.art
        route = self.route
        pages = art.pages
        tabs = Row([self._tab_button(label, tab) for tab, label in self.TABS],
                   gap=8)
        # Paginated toggle on the right (global setting, same as the library
        # filter bar) — a filter box could later sit beside it.
        tabbar = Row([tabs, Spacer(),
                      Checkbox(_("Paginated"), pages.enabled(),
                               id="music-paginated",
                               # "music-songs" is deliberately absent: the
                               # songs tab is UNPAGED, so its scroller is
                               # never torn down by this flip and forgetting
                               # it would jump the list to the top for
                               # nothing.
                               on_toggle=lambda: pages.toggle(route,
                                                              "music-grid"))],
                     pad=12, align="center")
        data = route.get("_data")
        tab = route.get("_tab")
        # Paginate the server-paged tile tabs. Songs is a list and genres is a
        # single unpaged request, so both keep scrolling; clear any page count
        # left over from an album tab or the bottom bar would linger.
        if pages.enabled() and data is not None and tab not in self.UNPAGED:
            return Column([tabbar,
                           self._paged_grid(size, art.geom_square, tabbar)],
                          flex=1, align="stretch")
        route.pop("_npages", None)
        if data is None:
            body = chrome.busy()
        elif tab == "songs":
            body = self._songs_body(data)
        else:
            body = self._grid_body(data, size, tab)
        return Column([tabbar, body], flex=1, align="stretch")

    def _tab_button(self, label, tab):
        return controls.tab_btn(label, "mtab-" + tab,
                                self.route.get("_tab", "albums") == tab,
                                lambda: self._set_tab(tab))

    def _songs_body(self, data):
        art = self.ctx.art
        route = self.route
        server = route.get("server") or self.ctx.server
        # The visible window, so the table fetches the rows it draws.
        head_h = chrome.CONTENT_PAD
        first, last = art.tiles.list_window(len(data), "music-songs", head_h)
        self._window(first, last)
        # art=True: this is a whole library's worth of songs from every
        # album at once, which is exactly the mixed-album case the art
        # column is for (the album page redundantly omits it). Safe
        # because the list is virtualized — only visible rows composite
        # an overlay, so the 63-overlay budget holds.
        return VScroll(Column([art.tiles.track_list(
            data, "song", self._play_songs_from,
            art=True, scroll_id="music-songs", menu=True)],
            pad=chrome.CONTENT_PAD, align="stretch"),
            id="music-songs", flex=1,
            offset=self.parked_scroll("music-songs"),
            on_scroll=lambda off, mx: art.scroll.on_scroll(
                "music-songs", off, mx,
                lambda o, m: art.pages.rewindow(route)))

    def _play_songs_from(self, index):
        """Play from a row of the songs tab, asking the SERVER for the queue.

        A music library's songs tab runs to thousands of rows and is
        windowed, so "everything on screen" is a handful of loaded pages
        with holes between them -- there is no full list to index into.

        jellyfin-web does not use its rendered rows either: shortcuts.js's
        playAllFromHere re-runs the container's own query and plays
        result.Items, falling back to scraped DOM ids only where the
        container has no fetcher. That re-fetch goes through
        getItemsForPlayback, i.e. the same Limit: 300 repository.QUEUE_LIMIT
        matches.

        Ours is the simpler shape, because the row index IS the track's
        absolute position in the library -- that is the invariant windowing
        is built on, so there is nothing to reconstruct from the DOM the way
        web has to. Ask for QUEUE_LIMIT songs starting there and play from
        the top of the answer.
        """
        from ..repository import QUEUE_LIMIT

        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server
        parent = route["parent_id"]

        def work():
            return source.get_songs(srv, parent, start_index=index,
                                    limit=QUEUE_LIMIT)

        def done(res):
            items, _total = res
            items = [i for i in items if i]
            if not items:
                return
            self.ctx.actions.play_list([i.get("Id") for i in items], srv, 0,
                                       audio=True, items=items)

        self.ctx.run.run(work, done, self.ctx.run.epoch,
                         on_error=lambda _e: self.ctx.status(
                             _("Could not start playback.")))

    def _grid_body(self, data, size, tab):
        art = self.ctx.art
        route = self.route
        # Every music tab is square, genres included. jellyfin-web renders a
        # MusicGenre as shape:'auto' with nothing to measure -- a genre carries
        # no Primary image -- and setCardData's no-aspect-ratio fallback is
        # square, so square is what it draws. The modern app says it outright:
        # Music -> SquareOverflow, everything else PortraitOverflow.
        geom = art.geom_square
        # Ask for the rows about to be drawn, from the SAME window the
        # renderer composites (TileRenderer.row_window). head_h is 0: the
        # tab bar lives outside this scroll, so the grid starts at its top.
        cols = art.tiles.cols(size[0], geom)
        first, last = art.tiles.row_window(size, geom, "music-grid")
        self._window(first * cols, (last + 1) * cols)
        return VScroll(
            Column(art.tiles.grid_of(data, "music", size, geom=geom,
                                     scroll_id="music-grid"),
                   pad=chrome.CONTENT_PAD, gap=GRID_GAP),
            id="music-grid", flex=1,
            offset=self.parked_scroll("music-grid"),
            # Row-snap the music library grid (see GridPage.render). The tabs
            # live outside this scroll, so the grid fills it: the first row
            # sits at the top pad, hence snap_off = CONTENT_PAD.
            snap=geom.strip_h + GRID_GAP,
            snap_off=chrome.CONTENT_PAD,
            # The callback is what lets a window that FAILED be asked for
            # again -- render cannot retry on its own without becoming a
            # request per frame. See Paginator.rewindow.
            on_scroll=lambda off, mx: art.scroll.on_scroll(
                "music-grid", off, mx,
                lambda o, m: art.pages.rewindow(route)))

    def _paged_grid(self, size, geom, tabbar):
        """One screenful of album/artist tiles, no scroll — the bottom
        pagination bar moves between pages. The tab bar sits above the grid
        (unlike the library grid's in-scroll header), so its measured height
        is what the page-size math reserves."""
        from ...mpvtk.layout import measure

        art = self.ctx.art
        head_h = measure(tabbar)[1]
        ps = art.pages.page_size(self.route, size, head_h, geom,
                                 chrome.CONTENT_PAD)
        page_items = art.pages.ensure(self.route, ps, self._page_fetcher(),
                                      seed=self.route.get("_data"))
        if page_items is None:
            rows = [Text(_("Loading…"), size=18, color=theme.SUBTLE_FG)]
        else:
            rows = art.tiles.grid_of(page_items, "music", size, geom=geom)
        return Column(rows, pad=chrome.CONTENT_PAD, gap=GRID_GAP,
                      align="stretch", flex=1)

    def _page_fetcher(self):
        """``fetch(start, limit)`` for a paginated music tab. Only the
        server-paged tile tabs reach here (songs/genres are handled without
        pagination in render)."""
        fetch = self._fetch_for_tab()
        return lambda start, limit: fetch(start, limit)

    # -- paging / tabs -----------------------------------------------------

    def _on_scroll_end(self, offset, maximum):
        """Page the SONGS tab in near the bottom.

        The tile tabs are windowed now (#617) and reach this from nothing;
        songs is a table whose rows are built by index and whose play action
        is built from the list, so it keeps appending until that is dealt
        with. See the work list's music section."""
        def put(r, items, total):
            r["_data"], r["_total"] = items, total

        fetch = self._fetch_for_tab()
        self.ctx.art.pages.more(
            self.route, offset, maximum,
            lambda r: (r.get("_data") or [], r.get("_total") or 0),
            put, lambda start: fetch(start))

    def _set_tab(self, tab):
        """Switch tab, keeping what each tab has already loaded.

        Every switch used to refetch from scratch, so flipping between
        Albums and Artists on a large library re-paged the whole thing each
        time. Tk cached per tab. The cache is per route dict, so it dies
        with the page and cannot go stale across a reload.
        """
        route = self.route
        cache = route.setdefault("_tab_cache", {})
        old_tab = route.get("_tab", "albums")
        if route.get("_data") is not None:
            cache[old_tab] = (route["_data"], route.get("_total"))
        route["_tab"] = tab
        for k in ("_data", "_total"):
            route.pop(k, None)
        route["_loading"] = False
        # Each tab is its own paginated result set (page 3 of Albums is not
        # page 3 of Artists); the item cache above is per tab, this isn't.
        self.ctx.art.pages.reset(route)
        # A new tab starts at the top; a stale offset would virtualize the
        # wrong window and show a screenful of blank rows.
        self.ctx.art.scroll.forget("music-grid", "music-songs")
        hit = cache.get(tab)
        if hit is not None:
            route["_data"], route["_total"] = hit
            self.ctx.run.bump()
            self.ctx.invalidate()
        else:
            self.ctx.nav.reload(route)
