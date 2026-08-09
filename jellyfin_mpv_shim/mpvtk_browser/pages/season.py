"""The season screen: a season picker, the shared actions, an episode grid."""

from ...i18n import _
from ...mpvtk.widgets import Column, Dropdown, Row, Text, VScroll
from ..components import chrome, controls, detail as detail_components
from ..tile_renderer import GRID_GAP
from .base import Page


class SeasonPage(Page):
    kind = "season"

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server

        def work():
            return {
                "episodes": source.get_episodes(
                    srv, route.get("series_id"), route["item_id"]),
                "seasons": source.get_seasons(srv, route.get("series_id")),
            }

        self.route_async(work, lambda d: route.__setitem__("_data", d), epoch)

    def render(self, size):
        art = self.ctx.art
        tiles = art.tiles
        actions = self.ctx.actions
        route = self.route
        data = route.get("_data")
        if data is None:
            return chrome.busy()
        episodes = data.get("episodes") or []
        seasons = data.get("seasons") or []
        server = route.get("server") or self.ctx.server
        geom = art.geom_wide   # episodes are landscape Thumb cards
        season_item: dict = next(
            (s for s in seasons if s.get("Id") == route["item_id"]), {})
        # Annotated: the picker Dropdown and the To Series button join a list
        # that starts with a Text, so mypy would infer list[Text] from it.
        title_row: list = [Text(route.get("title", ""), size="page", bold=True)]
        if len(seasons) > 1:
            names = [s.get("Name", "") for s in seasons]
            cur = next((i for i, s in enumerate(seasons)
                        if s.get("Id") == route["item_id"]), 0)
            title_row.append(Dropdown(
                "season-switch", names, selected=cur, w=220,
                on_select=lambda i, v: self._switch_season(seasons[i])))
        if route.get("series_id"):
            title_row.append(controls.action_btn(
                "movie", _("To Series"), "season-to-series",
                lambda: self.ctx.nav.navigate({
                    "kind": "series", "server": server,
                    "item_id": route["series_id"],
                    # `bar_title` is the same show name, and it is set from
                    # the item that got us here -- so it survives a season
                    # DTO that has no SeriesName, which would otherwise
                    # navigate with an empty title and leave the series
                    # page's bar reading "Home".
                    "title": (season_item.get("SeriesName")
                              or route.get("bar_title") or "")})))
        acts = []
        if route.get("series_id"):
            # Tk had Play Next Up here too. Landing on a season and being able
            # to carry on is the point of the screen; without it you had to go
            # up to the series page to resume.
            acts.append(controls.action_btn(
                "play_arrow", _("Next Up"), "se-nextup",
                lambda: actions.play_next_up(route["series_id"], server),
                primary=True))
        acts += detail_components.common_actions(
            actions, tiles,
            season_item or {"Id": route["item_id"], "Type": "Season"},
            server, "se")
        header = [Row(title_row, gap=12, align="center"),
                  Row(acts, gap=8, align="center")]
        rows = header + tiles.grid_of(
            episodes, "ep", size, geom=geom, image_type="Thumb",
            # inherit=False: a season listing is a list of *episodes*, so
            # every cell must be that episode's own still. The Thumb chain
            # otherwise borrows the series' thumb/backdrop -- correct for a
            # Continue Watching card, which is a pointer back to the show,
            # and useless here: it draws the same series artwork in every
            # cell of the grid and the screen stops distinguishing anything.
            #
            # Pinned by our own test rather than against jellyfin-web,
            # because web has no equivalent to compare with: it renders a
            # season's episodes as a *list view* with a leading image
            # (itemDetails/index.js:1423-1435), not as a card grid, so the
            # question of which artwork a season-page card takes never
            # arises there.
            inherit=False,
            scroll_id="season", head_h=100)
        return VScroll(Column(rows, pad=chrome.CONTENT_PAD, gap=GRID_GAP),
                       id="season", flex=1,
                       offset=self.parked_scroll("season"),
                       on_scroll=lambda off, mx: art.scroll.on_scroll(
                           "season", off, mx))

    def _switch_season(self, season):
        self.ctx.nav.navigate({
            "kind": "season",
            "server": self.route.get("server") or self.ctx.server,
            "item_id": season.get("Id"),
            "series_id": self.route.get("series_id"),
            "title": season.get("Name", ""),
            # Read from the season being switched *to*, then from the route
            # we are on: the picker's DTOs come from `get_seasons` and carry
            # SeriesName, but a season that is short the field must not
            # blank a bar that was correct a moment ago.
            "bar_title": (season.get("SeriesName")
                          or self.route.get("bar_title")),
        })
