"""The season screen: a season picker, the shared actions, an episode grid."""

from ...i18n import _
from ...mpvtk.widgets import Column, Dropdown, Row, Text, VScroll
from ..components import chrome, controls, detail as detail_components
from ..tile_renderer import GRID_GAP
from .base import Page


def _grouped(buttons, avail):
    """The header's buttons as ONE item for the title row, spaced at 8.
    ``[]`` for nothing, so a caller can add it unconditionally.

    Through ``wrap_row`` rather than a bare ``Row``: this group can be To
    Series plus a button per metadata database, which at a narrow window is
    wider than the page. ``wrap_row`` hands back a plain Row when it all
    fits -- the common case, and the same tree a bare Row would have built
    -- and a Column of rows when it does not, so the gap inside the group
    stays 8 at every width instead of degrading to the outer row's 12.

    Passed the FULL content width rather than what is left beside the
    season picker: the outer ``wrap_row`` will drop this whole item onto
    its own line if it does not fit next to the picker, and sizing the
    inner break to the remainder would wrap it early on the line it ends
    up having to itself.

    Module-level and tiny because it is a layout decision with one input and
    one output; on the page it would read as state.
    """
    from ..components import chrome

    if not buttons:
        return []
    return [chrome.wrap_row(buttons, avail, gap=8, align="center")]


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
        # The grid's own horizontal padding, needed BEFORE the banner: this
        # page is the first header caller whose column is not padded with
        # CONTENT_PAD, and `banner_box` sizes against that constant. With
        # `grid_fill: center` a centred grid can hand back a much larger
        # pad -- 94px at a 1200px window -- and the banner then overhangs
        # the column it sits in by ~100px and clips at the viewport.
        gpad, geom = tiles.grid_layout(size[0], geom)
        if tiles.header_bakes_heading(season_item):
            full_bleed = tiles.full_bleed_header(season_item)
            box = tiles.banner_box(size[0], full_bleed, size[1])
            if not full_bleed:
                # Full bleed leaves the padding entirely and is already the
                # viewport's width; only the padded box has to agree with a
                # pad that is not the one it assumed.
                from ...mpvtk.layout import SCROLLBAR_W

                box = (min(box[0], size[0] - 2 * gpad - SCROLLBAR_W), box[1])
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
        # Every BUTTON on this row, collected before any of them is placed.
        # To Series is one of them: the row's own gap separates the KINDS of
        # control on it (the title, the season picker, the buttons), and 8
        # is what the app puts between adjacent buttons -- the actions row
        # right below, and these same link buttons on detail and series. The
        # first version of this grouped only the links, which was the same
        # mistake one step in: on a season with a banner and no picker the
        # row is nothing BUT buttons, so the one wide gap was the only odd
        # spacing on a screen of 8s [iw].
        row_buttons: list = []
        if route.get("series_id"):
            row_buttons.append(controls.action_btn(
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
        # The provider links join that same button group, on the end of the
        # title row rather than a row of their own. This screen has two
        # bands above the grid already and the links would be a third, and a
        # page whose header is taller than its first row of episodes has
        # stopped being an episode list. Detail and series put them under
        # the synopsis, which is where web has them -- there is no synopsis
        # here.
        row_buttons += detail_components.provider_link_buttons(
            season_item, self.open_link)
        title_row += _grouped(row_buttons, size[0] - 2 * gpad)
        header = []
        if title_row:
            # wrap_row, not a bare Row: this one can now carry the season
            # picker, To Series and a provider button per database, and a
            # Row lets the tail run off the window. Measured against the
            # grid's own padding rather than CONTENT_PAD -- this page
            # centres its grid, so `gpad` can be 94px at a 1200px window.
            header.append(chrome.wrap_row(
                title_row, size[0] - 2 * gpad, gap=12, align="center"))
        header.append(Row(acts, gap=8, align="center"))
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
