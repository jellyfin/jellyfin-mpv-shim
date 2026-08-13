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
        # A season carries no backdrop of its own but does carry its
        # series' (ParentBackdropImageTags + ParentBackdropItemId, measured
        # against a real server -- `get_seasons` asks for no `Fields` and
        # they are on the DTO regardless), which `backdrop_spec` already
        # follows. So the header a series page draws is available here for
        # the asking, and the screen this one leads to -- the episode grid
        # -- was the one place in a show's chain that dropped to a bare line
        # of text [iw].
        #
        # The series' NAME goes in the context line above the title, which
        # is what makes "Season 1" mean something on a header whose artwork
        # belongs to the show rather than to the season.
        #
        # **No banner at all when there is no artwork**, where a detail or
        # series page still draws the placeholder panel. Those two ARE their
        # header -- the page is about one item and the panel is where its
        # name lives. This one is a grid with a title over it, and 400px of
        # empty grey between the two is a worse screen than the line of text
        # it replaced. Same reasoning `full_bleed_header` gives for refusing
        # to run a placeholder to the edges, one step further.
        #
        # Asked of the DTO rather than of the returned node, as
        # `backdrop_node` requires: a placeholder means "none" OR "not yet",
        # and reading it as "none" would move the picker and the whole grid
        # down the moment the artwork landed.
        title = route.get("title", "")
        banner = None
        full_bleed = False
        if tiles.header_bakes_heading(season_item):
            full_bleed = tiles.full_bleed_header(season_item)
            box = tiles.banner_box(size[0], full_bleed, size[1])
            banner = tiles.backdrop_node(
                season_item, box, "season-bd", title=title,
                meta=detail_components.meta_line(season_item) or None,
                context=(season_item.get("SeriesName")
                         or route.get("bar_title") or None))
        # Annotated: the picker Dropdown and the To Series button join a list
        # that may start with a Text, so mypy would infer list[Text] from it.
        title_row: list = []
        if banner is None:
            title_row.append(Text(title, size="page", bold=True))
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
        header = []
        if title_row:
            header.append(Row(title_row, gap=12, align="center"))
        header.append(Row(acts, gap=8, align="center"))
        gpad, geom = tiles.grid_layout(size[0], geom)
        # Measured, not the flat 100 this used to pass: head_h is what tells
        # the virtualizer which rows are near the viewport, and a header
        # that grew by 400px of artwork while the number stayed at 100
        # leaves the rows you are looking at un-composited.
        head_h = tiles.header_offset(
            header if banner is None else [banner] + header)
        if full_bleed:
            # header_offset assumes the flat padded column; full bleed is
            # the one that gives its top padding up (see chrome.header_body).
            head_h -= chrome.CONTENT_PAD
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
            scroll_id="season", head_h=head_h)
        # The plain column is not `header_body(None, ...)`: that helper is
        # about a page that OPENS with a banner, and giving it a None to
        # step over would put the question of whether there is one in two
        # places.
        body = (Column(rows, gap=GRID_GAP, pad=(gpad, chrome.CONTENT_PAD))
                if banner is None else
                chrome.header_body(banner, rows, gap=GRID_GAP,
                                   pad=(gpad, chrome.CONTENT_PAD),
                                   full_bleed=full_bleed))
        return VScroll(body,
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
