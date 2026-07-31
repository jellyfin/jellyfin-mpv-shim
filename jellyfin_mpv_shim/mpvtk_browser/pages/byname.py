"""Genre, studio and person screens — jellyfin-web's ``ItemsByName``.

A genre spans films and shows and albums; a studio spans films and shows; a
person spans everything they were in. One flat grid of all of it sorted by
name is a worse answer than a row per kind, which is what web draws and what
this is.

Very close to :mod:`favorites` on purpose, and deliberately not merged with
it. That screen's rows are a fixed set of types with fixed shapes because
"my favourites" *is* a list of types; these rows are whatever a particular
name happens to be attached to, and the set differs per name. The shared
part is four lines of tile_row call, and the unshared part is everything
about which rows exist and why.
"""

from ...i18n import _
from ...mpvtk.widgets import Column, VScroll
from ..components import chrome
from ..tile_renderer import GRID_GAP
from .base import Page


class ByNamePage(Page):
    kind = "byname"

    #: key -> (geom attribute, image type, inherit). jellyfin-web's
    #: itemsByName.js: films/shows/trailers portrait, episodes and videos
    #: backdrop, music square.
    #:
    #: Episodes inherit nothing (web passes no preferThumb there) for the
    #: reason a season grid does not either: this is a list of the episodes
    #: this person or genre is attached to, and drawing one series' thumb
    #: across all of them says nothing about any of them.
    ROW_SHAPES = {
        "movies": ("geom", "Primary", True),
        "shows": ("geom", "Primary", True),
        "episodes": ("geom_wide", "Thumb", False),
        "videos": ("geom_wide", "Thumb", True),
        "trailers": ("geom", "Primary", True),
        "albums": ("geom_square", "Primary", True),
        "songs": ("geom_square", "Primary", True),
    }

    def load(self, epoch):
        source, srv = self.ctx.source, self._srv()
        spec = self.route.get("list") or {}
        self.route_async(
            lambda: source.get_by_name_sections(srv, spec),
            lambda rows: self.route.__setitem__("_data", rows), epoch)

    def _srv(self):
        return self.route.get("server") or self.ctx.server

    def render(self, size):
        rows = self.route.get("_data")
        if rows is None:
            return chrome.busy()
        if not rows:
            return chrome.error(_("Nothing here yet."))
        art = self.ctx.art
        built = []
        for row in rows:
            attr, image_type, inherit = self.ROW_SHAPES.get(
                row["key"], ("geom", "Primary", True))
            built.append(art.tiles.tile_row(
                row["title"], row["items"], "byname-" + row["key"],
                geom=getattr(art, attr), image_type=image_type,
                inherit=inherit, bleed=True, see_all=self._see_all(row)))
        return VScroll(Column(built, pad=(0, chrome.CONTENT_PAD),
                              gap=GRID_GAP, align="stretch"),
                       id="byname", flex=1,
                       offset=self.parked_scroll("byname"),
                       on_scroll=lambda off, mx: art.scroll.on_scroll(
                           "byname", off, mx))

    def _see_all(self, row):
        """Only when the row is showing less than there is.

        jellyfin-web's rule for its own More button
        (``itemsByName.js:96``): a chevron onto a page that turns out to
        hold the same six tiles is a promise the destination cannot keep.
        The destination is the sortable, paged grid, so nothing is lost by
        the rows being capped.
        """
        total = row.get("total") or 0
        if total <= len(row["items"]):
            return None
        spec = dict(self.route.get("list") or {})
        spec["include_item_types"] = row.get("types")
        server = self._srv()
        title = row["title"]
        return lambda: self.ctx.nav.navigate({
            "kind": "list", "server": server, "title": title,
            "list": spec})
