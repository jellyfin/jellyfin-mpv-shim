"""The Favorites screen.

One row per item type, each of which is a top-N with a chevron into the
unbounded listing -- the same shape as the Live TV Programs tab, and for
the same reason: seven independent queries, any of which may be empty.

The shapes here are jellyfin-web's ``favoriteitems.js`` table, which is the
clearest statement of type-to-shape intent anywhere in that codebase. It is
also the only place it is written down: the library grid infers shape from
artwork (see ``GridPage._grid_shape``) and never consults item type at all.
So this is a deliberate exception rather than a second implementation of
the same rule -- a favourites row is *defined* by its type, which makes the
type the honest thing to shape it by.
"""

from ...i18n import _
from ...mpvtk.widgets import Column, VScroll
from ..components import chrome
from ..tile_renderer import GRID_GAP
from .base import Page


class FavoritesPage(Page):
    kind = "favorites"

    #: key -> (geom attribute, image type). jellyfin-web's favoriteitems.js:
    #: movies/shows portrait, episodes and videos backdrop, music square.
    #:
    #: Episodes are ``preferThumb: false`` there -- the episode's own image,
    #: not the show's. A favourites list is a list of the things you marked,
    #: so a row of one series' episodes all wearing the series thumb would
    #: defeat the screen. That is ``inherit=False`` here, the same lever the
    #: season grid uses.
    ROW_SHAPES = {
        "movies": ("geom", "Primary", True),
        "shows": ("geom", "Primary", True),
        "episodes": ("geom_wide", "Thumb", False),
        "videos": ("geom_wide", "Thumb", True),
        "artists": ("geom_square", "Primary", True),
        "albums": ("geom_square", "Primary", True),
        "songs": ("geom_square", "Primary", True),
    }

    def load(self, epoch):
        source, srv = self.ctx.source, self._srv()
        self.route_async(
            lambda: source.get_favorite_sections(srv),
            lambda rows: self.route.__setitem__("_data", rows), epoch)

    def _srv(self):
        return self.route.get("server") or self.ctx.server

    def render(self, size):
        rows = self.route.get("_data")
        if rows is None:
            return chrome.busy()
        if not rows:
            return chrome.error(
                _("Nothing is marked as a favourite yet."))
        art = self.ctx.art
        built = []
        for row in rows:
            attr, image_type, inherit = self.ROW_SHAPES.get(
                row["key"], ("geom", "Primary", True))
            built.append(art.tiles.tile_row(
                row["title"], row["items"], "fav-" + row["key"],
                geom=getattr(art, attr), image_type=image_type,
                inherit=inherit, bleed=True,
                see_all=self._see_all(row)))
        return VScroll(Column(built, pad=(0, chrome.CONTENT_PAD),
                              gap=GRID_GAP, align="stretch"),
                       id="favorites", flex=1,
                       offset=self.parked_scroll("favorites"),
                       on_scroll=lambda off, mx: art.scroll.on_scroll(
                           "favorites", off, mx))

    def _see_all(self, row):
        """The row's own query without the limit -- the same predicate, so
        the listing cannot drift from the row that links to it."""
        server = self._srv()
        title = row["title"]
        types = row.get("types")
        if not types:
            return None
        return lambda: self.ctx.nav.navigate({
            "kind": "list", "server": server, "title": title,
            "list": {"type": "items", "is_favorite": True,
                     "include_item_types": types}})
