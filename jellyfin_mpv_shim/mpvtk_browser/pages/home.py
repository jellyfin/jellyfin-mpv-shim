"""The home screen.

Body moved verbatim from ``ViewsMixin._load_home`` / ``_render_home``, plus
the two helpers only this screen used (``_row_shape``, ``_order_rows``) —
which is the point of the conversion: they were methods on a 348-method
object because there was nowhere else to put them, and they belong to one
screen.

Reaches the shell for nothing. The first page to manage that.
"""

from ...i18n import _
from ...mpvtk.widgets import (
    Box, Busy, Button, Column, Row, Spacer, Text, VScroll)
from .. import components, home_sections, theme
from ..components import chrome
from .base import Page


class HomePage(Page):
    kind = "home"

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
        """
        route = self.route
        source = self.ctx.source
        run = self.ctx.run
        invalidate = self.ctx.invalidate

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
            if run.epoch == epoch:
                route["_data"] = {"libraries": libs, "layout": layout,
                                  "rows": self._order_rows(primary)}
                invalidate()
            latest = rows("latest")
            return {"libraries": libs, "layout": layout,
                    "rows": self._order_rows(primary + latest)}

        self.route_async(work, lambda d: route.__setitem__("_data", d), epoch)

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
                      Text(_("Connecting to your server…"), size=20,
                           color=theme.SUBTLE_FG),
                      Spacer()]),
                 Spacer()],
                flex=1, direction="column", align="stretch", gap=16)
        data = route.get("_data")
        if data is None:
            return chrome.busy()
        layout = data.get("layout") or list(home_sections.DEFAULT_LAYOUT)
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
                            art.geom_wide, "Primary", "row-libs", False))
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
                            self._latest_tv(hr)))
        entries.sort(key=lambda e: e[0])
        rows = []
        for _slot, title, items, geom, itype, row_id, pitem in entries:
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
                                           parent_item=pitem))
        if not rows:
            rows.append(Row([Spacer(w=chrome.CONTENT_PAD),
                             Text(_("Nothing to show yet."), size=20,
                                  color=theme.SUBTLE_FG)]))
        # pad=0: home carousels bleed to the window edges so their page
        # arrows sit flush against them (see TileRenderer.hscroll_row).
        #
        # Snap vertical scroll to section headings: the home screen is
        # bitmap-heavy (a wide carousel strip per section) and scrolled fast,
        # so a continuous offset repositions every section every frame. Section
        # heights differ (poster vs landscape rows), so the breakpoints are the
        # explicit content-y of each section top, not a uniform pitch.
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
             Text(_("Live TV"), size=(theme.active() or {}).get(
                 "heading_size", 24), bold=True)]
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
