"""The search results screen — first route converted to a :class:`Page`.

Chosen to go first because it is representative without being large: it has
both a loader and a renderer, it fans results into typed groups (so it
exercises the art context), and it is one of the seven screens pinned by
``tests/test_scene_snapshots.py`` — which is what makes "the move changed
nothing" checkable rather than asserted.

Body is moved verbatim from ``ViewsMixin._load_search`` / ``_render_search``.
The only edits are ``self.X`` -> ``self.ctx.X``. It originally spent nine
``ctx.shell`` uses -- the entire budget -- on the tile and chrome helpers;
step 6c's two prep commits gave every one of them a real home, and it now
reaches the shell for nothing.
"""

from ...i18n import _
from ...mpvtk.widgets import Column, Text, VScroll
from .. import theme
from ..components import chrome
from .base import Page


class SearchPage(Page):
    kind = "search"

    def load(self, epoch):
        srv = self.route.get("server") or self.ctx.server
        term = self.route.get("term", "")
        source = self.ctx.source
        route = self.route

        def work():
            if not term:
                return {"items": [], "people": []}
            items = source.search(srv, term)
            people = []
            try:
                people = source.search_people(srv, term)
            except Exception:
                pass
            return {"items": items, "people": people}

        self.route_async(work, lambda d: route.__setitem__("_data", d), epoch)

    def render(self, size):
        art = self.ctx.art
        tiles = art.tiles
        route = self.route
        term = route.get("term", "")
        if not term:
            return chrome.error(_("Type in the search box above."))
        data = route.get("_data")
        if data is None:
            return chrome.busy()
        items = data.get("items") or []
        people = data.get("people") or []
        rows = [Text(_('Results for "%s"') % term, size=24, bold=True)]
        if people:
            rows.append(tiles.tile_row(_("People"), people, "search-people",
                                       geom=art.geom))
        # Group by type, each with its natural tile shape (like the Tk browser).
        groups = [
            (_("Movies"), ("Movie",), art.geom, "Primary"),
            (_("Shows"), ("Series",), art.geom, "Primary"),
            (_("Episodes"), ("Episode",), art.geom_wide, "Thumb"),
            (_("Videos"), ("Video", "MusicVideo"), art.geom_wide, "Primary"),
            (_("Albums"), ("MusicAlbum",), art.geom_square, "Primary"),
            (_("Artists"), ("MusicArtist",), art.geom_square, "Primary"),
        ]
        used = set()
        for label, types_, geom, itype in groups:
            group = [it for it in items if it.get("Type") in types_]
            if group:
                used.update(types_)
                rows.append(tiles.tile_row(
                    label, group, "search-" + label, geom=geom,
                    image_type=itype))
        songs = [it for it in items if it.get("Type") == "Audio"]
        if songs:
            server = route.get("server") or self.ctx.server
            ids = [s.get("Id") for s in songs]
            rows.append(Text(_("Songs"), size=24, bold=True))
            # Deliberately NOT virtualized. Virtualizing needs head_h — the
            # height of everything above the table — to map a scroll offset
            # onto a row, and here that is the People row plus up to six
            # carousels, i.e. not knowable at build time. The old fixed 120
            # was out by roughly 10x, and the VScroll had no on_scroll at all,
            # so the window computed at offset 0 was the only one ever
            # materialized: every song past the first screenful drew blank,
            # permanently. Search is capped at 60 results across all types and
            # this table has no art cells, so there is nothing to virtualize
            # away — no overlays, just text rows.
            rows.append(tiles.track_list(
                songs, "search-song",
                lambda i: self.ctx.actions.play_list(ids, server, i,
                                                     audio=True),
                menu=True))
        other = [it for it in items
                 if it.get("Type") not in used and it.get("Type") != "Audio"]
        if other:
            rows.append(tiles.tile_row(_("Other"), other, "search-other"))
        if not items and not people:
            rows.append(Text(_("No results."), size=18, color=theme.SUBTLE_FG))
        return VScroll(Column(rows, pad=chrome.CONTENT_PAD, gap=12,
                              align="stretch"), id="search", flex=1,
                       offset=self.parked_scroll("search"))
