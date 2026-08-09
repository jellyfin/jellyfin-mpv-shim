"""The home screen.

Body moved verbatim from ``ViewsMixin._load_home`` / ``_render_home``, plus
the two helpers only this screen used (``_row_shape``, ``_order_rows``) —
which is the point of the conversion: they were methods on a 348-method
object because there was nowhere else to put them, and they belong to one
screen.

Reaches the shell for nothing. The first page to manage that.
"""

import logging

from ...i18n import _
from ...mpvtk.widgets import (
    Box, Busy, Button, Column, Row, Spacer, Text, VScroll)
from .. import components, home_sections, theme, user_prefs
from ..components import chrome
from .base import Page

log = logging.getLogger("mpvtk_browser.pages.home")


class HomePage(Page):
    kind = "home"

    #: Sections the episode-images preference applies to, matching the set
    #: jellyfin-web passes ``inheritThumb: !useEpisodeImages`` to
    #: (``homesections/sections/nextUp.ts:119``, ``resume.ts:111``, which
    #: dispatches all three resume kinds).
    #:
    #: **LATEST is deliberately absent, and that is not an oversight.** A
    #: Latest-Episodes row sets ``preferThumb`` with no ``inheritThumb`` at
    #: all (``utils/sections.ts:135-144``), so it *always* prefers the series
    #: artwork and the setting does not reach it. We get there by a different
    #: route -- ``_latest_tv`` turns those rows into ParentPrimary tiles
    #: captioned with the show's name -- and folding LATEST in here would
    #: make turning the setting on scatter episode stills through a row that
    #: is a list of shows.
    EPISODE_IMAGE_ROWS = frozenset({
        home_sections.RESUME,
        home_sections.RESUME_AUDIO,
        home_sections.NEXT_UP,
    })

    # -- load --------------------------------------------------------------

    def load(self, epoch):
        """Two batches: draw the top of the page, then fill in Latest.

        The Latest rows are one request per library and sit below the fold,
        so waiting for them gated first paint on content nobody has scrolled
        to yet. Libraries + Continue Watching + Next Up now publish as soon as
        they land, and the Latest rows replace that partial data when they
        arrive.

        The batches are merged by slot rather than concatenated: the user can
        put Recently Added above Continue Watching, so "primary then latest"
        is no longer the display order.

        **The partial batch is published only on a first paint.** This loader
        also runs for #560's refreshes, which fire on a UserDataChanged burst
        while the user is looking at the screen -- and publishing
        primary-only there takes the Latest rows *away* for the length of one
        request per library before putting them back. "Load, not reload"
        promised the screen keeps what it has; a partial publish breaks that
        promise more visibly than a spinner would.
        """
        route = self.route
        source = self.ctx.source
        run = self.ctx.run
        invalidate = self.ctx.invalidate

        # This screen's rows *are* watched state, and the copy the catalog
        # holds for offline browsing is only refreshed on a background
        # tick. Ask for that pull now, so an episode finished on another
        # device is current here -- and, more to the point, is still
        # current when the network goes away a moment later. Non-blocking:
        # it marks the pull due and wakes the sync worker.
        refresh = getattr(self.ctx.player, "refresh_downloaded_userdata",
                          None)
        if refresh is not None:
            try:
                refresh()
            except Exception:
                log.debug("could not request a userdata refresh",
                          exc_info=True)

        def work():
            server = route.get("server") or self.ctx.server
            # getattr, not a plain call: this is the path the offline
            # fallback lands in, and a source without this method must
            # degrade to the stock layout rather than raise. An exception
            # here re-triggers the fallback that got us here, which is the
            # unbounded retry loop get_home_rows' docstring warns about.
            get_prefs = getattr(source, "get_home_prefs", None)
            layout, excludes = (get_prefs(server) if get_prefs
                                else (list(home_sections.DEFAULT_LAYOUT),
                                      frozenset()))
            # getattr for the same reason as get_home_prefs above: the
            # offline source has no display preferences and must fall back
            # to the defaults rather than raise from inside the fan-out.
            get_user = getattr(source, "get_user_prefs", None)
            inherit = user_prefs.inherit_artwork(
                get_user(server) if get_user else None)
            libs = source.get_libraries(server)

            def rows(stage):
                return source.get_home_rows(
                    server, libs, sections=(stage,), layout=layout,
                    latest_excludes=excludes)

            primary = rows("primary")
            # Epoch-checked by hand: this publishes mid-flight, so it is not
            # covered by the run_async gate that protects the final result.
            # Without it, navigating away mid-load would repaint the home
            # screen the user just left.
            if run.epoch == epoch and route.get("_data") is None:
                route["_data"] = {"libraries": libs, "layout": layout,
                                  "inherit": inherit,
                                  "rows": self._order_rows(primary)}
                invalidate()
            latest = rows("latest")
            return {"libraries": libs, "layout": layout,
                    "inherit": inherit,
                    "rows": self._order_rows(primary + latest)}

        self.route_async(work, self._publish, epoch)

    def _publish(self, data):
        """Adopt a loaded home screen, unless it is what is already there.

        A UserDataChanged burst re-reads this screen every few seconds and
        most of those change nothing it draws -- someone else's play position
        on an item in no row of yours, or the same item in the same place.
        Rebuilding the tree for that costs a full home layout, and the rows
        are on screen while it happens.
        """
        if self.route.get("_data") == data:
            return
        self.route["_data"] = data

    #: Rows with a full listing behind them, by section kind. Only Next Up
    #: for now; jellyfin-web also links Latest and On Now, which need their
    #: own list specs.
    #:
    #: Everything absent is absent on purpose, and matches web: Continue
    #: Watching, Continue Listening, Active Recordings and My Media have no
    #: chevron in any layout. A resume row is not the top of a longer list --
    #: it *is* the list -- and "see all of what you have not finished" is not
    #: a screen anyone asked for.
    SEE_ALL_ROWS = {
        home_sections.NEXT_UP: {"type": "nextup"},
        # jellyfin-web's On Now chevron is #/list?type=Programs&IsAiring=true
        # -- the plain guide query, not the recommendations endpoint the row
        # itself uses. Same destination here.
        home_sections.LIVE_TV: {"type": "programs",
                                "filters": {"is_airing": True}},
    }

    #: Sort index for "Date Added", descending. Named because a Latest row's
    #: destination is its library in that order, and a bare 1 in the middle
    #: of a navigate() says nothing.
    _DATE_ADDED_SORT = 1

    def _see_all(self, row_id, title, row=None):
        """``on_click`` for a row's heading, or None if it has no listing."""
        kind = row_id[4:].rsplit("-", 1)[0] if row_id.startswith("row-") else ""
        server = self.route.get("server") or self.ctx.server
        if kind == home_sections.LATEST and (row or {}).get("parent_id"):
            # Web opens the library on its Latest *tab*; we have no tabs, so
            # the honest equivalent is that library sorted newest-first --
            # the same items in the same order, without the 16-item cap.
            return lambda: self.ctx.nav.navigate({
                "kind": "grid", "server": server,
                "parent_id": row["parent_id"],
                "collection_type": row.get("collection_type"),
                "title": title, "_sort": self._DATE_ADDED_SORT})
        spec = self.SEE_ALL_ROWS.get(kind)
        if spec is None:
            return None
        return lambda: self.ctx.nav.navigate({
            "kind": "list", "server": server, "title": title,
            "list": dict(spec)})

    @staticmethod
    def _order_rows(rows):
        """Restore the user's section order across the two fetch batches.

        Stable, so the per-library Latest rows keep the library order they
        were submitted in rather than shuffling within their slot.
        """
        return sorted(rows, key=lambda r: r.get("slot", 0))

    # -- render ------------------------------------------------------------

    def render(self, size):
        art = self.ctx.art
        route = self.route
        if self.ctx.server is None:
            return Box(
                [Spacer(),
                 Row([Spacer(), Busy(), Spacer()]),
                 Row([Spacer(),
                      Text(_("Connecting to your server…"), size="large",
                           color=theme.SUBTLE_FG),
                      Spacer()]),
                 Spacer()],
                flex=1, direction="column", align="stretch", gap=16)
        data = route.get("_data")
        if data is None:
            return chrome.busy()
        layout = data.get("layout") or list(home_sections.DEFAULT_LAYOUT)
        # Whether a resume/next-up tile may borrow the series' artwork; every
        # other row always may. Defaults to True when the row data predates
        # the key (an offline fallback, or a partial first paint).
        inherit = data.get("inherit", True)
        # The Libraries row is a configurable section now, not a fixed header,
        # so it is placed by slot alongside the fetched rows. Its slot may be
        # absent entirely (the user set every slot to something else), in
        # which case the home screen simply has no library row — the sidebar
        # and search still reach them.
        entries = []
        if data["libraries"] and home_sections.LIBRARIES in layout:
            entries.append((layout.index(home_sections.LIBRARIES),
                            _("Libraries"), data["libraries"],
                            # Libraries read as landscape cards, like the web
                            # client.
                            art.geom_wide, "Primary", "row-libs", False, True,
                            None))
        # Ids are derived from section kind and ordinal, not from position:
        # they key the scroll containers, so an index-based id would hand a
        # reordered section the previous occupant's scroll offset.
        seen: dict = {}
        for hr in data["rows"]:
            if not hr.get("items"):
                continue
            kind = hr.get("kind") or "row"
            n = seen[kind] = seen.get(kind, -1) + 1
            geom, itype = self._row_shape(hr)
            entries.append((hr.get("slot", 0), hr["title"], hr["items"],
                            geom, itype, "row-%s-%d" % (kind, n),
                            self._latest_tv(hr),
                            inherit if kind in self.EPISODE_IMAGE_ROWS
                            else True,
                            self._see_all("row-%s-%d" % (kind, n),
                                          hr["title"], hr)))
        entries.sort(key=lambda e: e[0])
        rows = []
        for (_slot, title, items, geom, itype, row_id, pitem, inh,
             see_all) in entries:
            # "-0": the FIRST Live TV row only. Nothing stops a layout from
            # holding the section twice, and a second button row would
            # duplicate every node id in it — the renderer then targets only
            # the last occurrence, so the first row's buttons would be dead.
            if row_id == "row-%s-0" % home_sections.LIVE_TV:
                # jellyfin-web's Live TV home section is a row of buttons
                # into the six Live TV screens *plus* the On Now strip. The
                # strip alone is what the shim used to draw, which left the
                # guide and the recordings reachable only by finding the
                # Live TV library tile.
                rows.append(self._live_tv_buttons())
            rows.append(art.tiles.tile_row(title, items, row_id, geom=geom,
                                           image_type=itype, bleed=True,
                                           parent_item=pitem, inherit=inh,
                                           see_all=see_all))
        if not rows:
            rows.append(Row([Spacer(w=chrome.CONTENT_PAD),
                             Text(_("Nothing to show yet."), size="large",
                                  color=theme.SUBTLE_FG)]))
        # pad=0: home carousels bleed to the window edges so their page
        # arrows sit flush against them (see TileRenderer.hscroll_row).
        #
        # Declare where the sections START, so the renderer can align to
        # them. It does not always: alignment is applied when a gesture
        # outruns what a frame measurably costs, or when scroll_mode asks for
        # it (widgets.Scroll, state.rcost) -- so this costs nothing on a
        # machine that keeps up. It is declared because the home screen is
        # bitmap-heavy, a wide carousel strip per section, and on one that
        # does not keep up a continuous offset repositions every section
        # every frame. Section heights differ (poster vs landscape rows), so
        # the breakpoints are the explicit content-y of each section top,
        # not a uniform pitch.
        return VScroll(Column(rows, gap=20), id="home", flex=1,
                       offset=self.parked_scroll("home"),
                       snaps=components.section_offsets(rows, 20))

    def _live_tv_buttons(self):
        """The Live TV section's nav row: one button per Live TV tab.

        They land on the same page the library tile opens, with the tab
        pre-selected — which is why the tab key travels on the route rather
        than being a separate screen each.
        """
        from .livetv import LiveTvPage

        return Row(
            [Spacer(w=chrome.CONTENT_PAD),
             Text(_("Live TV"), size=theme.heading_size(), bold=True)]
            + [Button(label, id="home-lt-" + key,
                      on_click=lambda k=key: self.ctx.nav.navigate({
                          "kind": "livetv", "server": self.ctx.server,
                          "title": _("Live TV"), "_tab": k}))
               for key, label in LiveTvPage.TABS],
            gap=8, align="center")

    @staticmethod
    def _latest_tv(hr):
        """A "Latest" row for a TV library.

        The server answers these with a *mix* of Series (a show that got
        several new episodes) and bare Episodes (a show that got one), so the
        row reads as a list of shows with an episode dropped into the middle
        of it. The Episodes are therefore drawn as their series — see
        ``TileRenderer._tile``. The row's shape is not affected: these stay
        poster rows like every other TV row.
        """
        return (hr.get("kind") == home_sections.LATEST
                and hr.get("collection_type") == "tvshows")

    def _row_shape(self, hr):
        """(geom, image_type) for a home row, classified like the Tk browser:
        movies/tv/boxsets and Live TV -> poster; music/playlists -> square;
        home-video/misc or episode-bearing rows -> landscape Thumb."""
        art = self.ctx.art
        ctype = hr.get("collection_type")
        items = hr.get("items", [])
        has_episode = any(it.get("Type") == "Episode" for it in items)
        if ctype == "livetv":
            # Shaped by what the programmes' artwork actually is, exactly as
            # jellyfin-web's card builder does it (see
            # TileRenderer.auto_geom) — a row of films with posters comes out
            # as posters, a row of guide stills as landscape. Landscape is
            # the fallback when nothing carries a ratio, because most guide
            # entries have no art of their own and borrow the channel logo,
            # which survives a 16:9 crop and does not survive a 2:3 one.
            return art.tiles.auto_geom(items, default=art.geom_wide,
                                       default_type="Thumb")
        if ctype in ("movies", "tvshows", "boxsets"):
            return art.geom, "Primary"
        if ctype == "books":
            # Poster. The Continue Reading row is the one resume row
            # jellyfin-web does not shape 16:9, and a Latest row from a
            # books library is covers either way. A books library also holds
            # AUDIOBOOKS, whose art is often square -- but a row is one
            # shape, and cropping a square cover into a 2:3 frame loses less
            # than letterboxing a portrait one into 16:9.
            return art.geom, "Primary"
        if ctype in ("music", "playlists"):
            return art.geom_square, "Primary"
        # An untyped row of playlists/music (offline, the mixed rows) still
        # gets square art.
        if art.tiles.square_geom(items):
            return art.geom_square, "Primary"
        if ctype:
            return art.geom_wide, ("Thumb" if has_episode else "Primary")
        if has_episode:
            return art.geom_wide, "Thumb"
        return art.geom, "Primary"
