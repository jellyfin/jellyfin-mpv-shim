"""The series screen: banner, actions, season carousel, cast.

``_series_actions`` was private to this route and comes with it. The Watched
/ Favorite / Download trio does not — it is shared with detail, season and
playlist, so it lives in ``components/detail.py``.
"""

from ...i18n import _
from ...mpvtk.widgets import Row, Text, VScroll
from .. import theme
from ..components import chrome, controls, detail as detail_components
from .base import Page


class SeriesPage(Page):
    kind = "series"

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server
        iid = route["item_id"]

        def work():
            similar: list = []
            try:
                similar = source.get_similar(srv, iid) or []
            except Exception:
                pass   # offline / older server: just no row
            trailers: list = []
            try:
                trailers = source.get_trailers(srv, iid) or []
            except Exception:
                pass   # older servers / no trailers: just no button
            return {
                "item": source.get_item(srv, iid),
                "seasons": source.get_seasons(srv, iid),
                "similar": similar, "trailers": trailers,
            }

        self.route_async(work, lambda d: route.__setitem__("_data", d), epoch)

    def render(self, size):
        tiles = self.ctx.art.tiles
        route = self.route
        data = route.get("_data")
        if data is None:
            return chrome.busy()
        item = data.get("item") or {}
        w = size[0]
        full_bleed = tiles.full_bleed_header(item)
        bw, bh = tiles.banner_box(w, full_bleed, size[1])
        server = route.get("server") or self.ctx.server
        meta = detail_components.meta_line(item)
        banner = tiles.backdrop_node(item, (bw, bh), "series-bd",
                                     title=item.get("Name", ""), meta=meta)
        blocks = [banner]
        if not tiles.header_bakes_heading(item):
            # Asked of the DTO, not of the node — see DetailPage.render and
            # `backdrop_node`: a placeholder means "none" or "not yet", and
            # drawing the heading here in the second case shifted the
            # actions row down until the image arrived.
            blocks.append(Text(item.get("Name", ""), size="hero", bold=True))
            if meta:
                blocks.append(Text(meta, size="large", color=theme.SUBTLE_FG))
        blocks.append(self._series_actions(item, server, route["item_id"],
                                           trailers=data.get("trailers")))
        if item.get("Overview"):
            blocks.append(chrome.paragraph(item["Overview"], 18,
                                           tiles.body_w(w)))
        # Same place as the detail screen: the tail of the metadata, above
        # the rows. Free here -- this page loads through `get_item`, and the
        # single-item routes fill ExternalUrls in without being asked.
        links = detail_components.provider_links(item, tiles.body_w(w),
                                                 self.open_link,
                                                 web_url=self.web_url(item))
        if links is not None:
            blocks.append(links)
        seasons = data.get("seasons") or []
        if seasons:
            blocks.append(tiles.tile_row(_("Seasons"), seasons,
                                         "series-seasons"))
        people = detail_components.people_row(tiles, item.get("People") or [])
        if people is not None:
            blocks.append(people)
        if data.get("similar"):
            # Shaped by its own artwork, like jellyfin-web
            # (shape: autooverflow, itemDetails/index.js:1245).
            # "More Like This" for a film is posters and for a
            # music album is square covers; one fixed poster row
            # cropped the second case. Poster stays the fallback
            # for a row where nothing carries an aspect ratio.
            sim_geom, sim_type = tiles.auto_geom(
                data["similar"], default=tiles.art.geom,
                default_type="Primary")
            blocks.append(tiles.tile_row(
                _("More Like This"), data["similar"], "series-similar",
                geom=sim_geom, image_type=sim_type))
        return VScroll(chrome.header_body(blocks[0], blocks[1:], gap=16,
                                          full_bleed=full_bleed),
                       id="series", flex=1,
                       offset=self.parked_scroll("series"))

    def _series_actions(self, item, server, series_id, trailers=None):
        actions = self.ctx.actions
        btns = [controls.action_btn(
            "play_arrow", _("Next Up"), "sa-nextup",
            lambda: actions.play_next_up(series_id, server), primary=True),
            controls.action_btn(
                "shuffle", _("Shuffle"), "sa-shuffle",
                lambda: actions.shuffle_series(series_id, server))]
        # The detail loader had always fetched trailers for a Series, but a
        # Series routes here, which had no button — one wasted API call per
        # load, for a feature nobody could reach.
        tids = [t.get("Id") for t in (trailers or []) if t.get("Id")]
        if tids:
            btns.append(controls.action_btn(
                "movie", _("Trailer"), "sa-trailer",
                lambda: actions.play_list(tids, server, 0)))
        btns += detail_components.common_actions(
            actions, self.ctx.art.tiles, item, server, "sa")
        return Row(btns, gap=8, align="center")
