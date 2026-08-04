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

#: How many results any one row shows.
#:
#: The query asks for a lot (repository.SEARCH_LIMIT) so that every type is
#: represented -- that is the half of #641 about a movie never appearing
#: because episodes ate a shared budget of 60. This is the other half, and
#: the issue asks for it in as many words: "works best with low limit per
#: type". A row is a horizontal carousel that composites every tile it is
#: given, and the Songs table below is deliberately not virtualized, so
#: without a per-row cap a term matching 700 songs lays out thousands of
#: nodes and blows past mpv's 63-overlay budget. 60 is what the whole
#: screen used to be capped at, now applied per row instead of across them.
ROW_MAX = 60


class SearchPage(Page):
    kind = "search"

    def load(self, epoch):
        srv = self.route.get("server") or self.ctx.server
        term = self.route.get("term", "")
        source = self.ctx.source
        route = self.route

        def work():
            if not term:
                return {"items": [], "people": [], "live": {}}
            items = source.search(srv, term)
            people = []
            try:
                people = source.search_people(srv, term)
            except Exception:
                pass
            # Live TV is searched separately — its items are not in the
            # /Search/Hints media types — and only where there is a tuner,
            # so the overwhelming majority of users pay nothing for it. Both
            # halves are getattr'd: the offline source has neither.
            live = {}
            if getattr(source, "has_live_tv", lambda _s: False)(srv):
                search_live = getattr(source, "search_live_tv", None)
                if search_live is not None:
                    live = search_live(srv, term) or {}
            return {"items": items, "people": people, "live": live}

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
        # Searching is a keyboard gesture even from a remote (the search
        # button puts the cursor in the box), so the results land focused
        # on the first of them — otherwise submitting leaves focus in the
        # box, where the arrow keys still move the caret. Whichever row
        # comes first owns it; `first` is claimed by the first row built.
        first = [True]

        def claim():
            got, first[0] = first[0], False
            return got

        if people:
            rows.append(tiles.tile_row(_("People"), people[:ROW_MAX],
                                       "search-people",
                                       geom=art.geom,
                                       autofocus_first=claim()))
        # Group by type, each with its natural tile shape (like the Tk browser).
        # The last column is ``inherit``: whether a tile may fall back to its
        # series' artwork. Episodes say no -- a result row of episodes is
        # about the episodes, and inheriting draws one show's thumb over
        # several of them, which is the same thing that made a season grid
        # useless. jellyfin-web gets there differently (its search Episodes
        # row sets no preferThumb at all, so it lands on the episode's own
        # Primary); asking for a *landscape* image of the episode first is
        # the same intent in a shape that fits the tile.
        groups = [
            (_("Movies"), ("Movie",), art.geom, "Primary", True),
            (_("Shows"), ("Series",), art.geom, "Primary", True),
            (_("Episodes"), ("Episode",), art.geom_wide, "Thumb", False),
            (_("Videos"), ("Video", "MusicVideo"), art.geom_wide, "Primary",
             True),
            (_("Albums"), ("MusicAlbum",), art.geom_square, "Primary", True),
            (_("Artists"), ("MusicArtist",), art.geom_square, "Primary", True),
        ]
        used = set()
        for label, types_, geom, itype, inherit in groups:
            group = [it for it in items if it.get("Type") in types_][:ROW_MAX]
            if group:
                used.update(types_)
                rows.append(tiles.tile_row(
                    label, group, "search-" + label, geom=geom,
                    image_type=itype, inherit=inherit,
                    autofocus_first=claim()))
        songs = [it for it in items if it.get("Type") == "Audio"][:ROW_MAX]
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
            # permanently. The table is bounded by ROW_MAX instead (the
            # whole-screen cap of 60 became a per-row one when the query was
            # widened for #641 — widening it without that would have left
            # this table drawing seven hundred rows), and it has no art
            # cells, so there is nothing to virtualize away: no overlays,
            # just text rows.
            #
            # **And art=True must stay off here**, tempting as it is: these
            # results span every album, which is exactly the mixed-album case
            # the art column is for, and jellyfin-web draws them as square
            # cards *with* covers. But an art cell is one mpv overlay per
            # visible row, the budget is 63 (renderer.lua MAX_OVERLAYS), and
            # ROW_MAX songs plus the ten carousels above them is already past
            # it — with no virtualization to trim the list, as above.
            # The failure would not be subtle or local: overlays simply stop
            # appearing, anywhere on the screen.
            rows.append(tiles.track_list(
                songs, "search-song",
                lambda i: self.ctx.actions.play_list(ids, server, i,
                                                     audio=True),
                menu=True))
        live = data.get("live") or {}
        if live.get("channels"):
            rows.append(tiles.tile_row(_("Channels"), live["channels"],
                                       "search-channels",
                                       geom=art.geom_square))
        if live.get("programs"):
            rows.append(tiles.tile_row(_("On TV"), live["programs"],
                                       "search-programs", geom=art.geom_wide,
                                       image_type="Thumb"))
        other = [it for it in items
                 if it.get("Type") not in used
                 and it.get("Type") != "Audio"][:ROW_MAX]
        if other:
            rows.append(tiles.tile_row(_("Other"), other, "search-other"))
        if not items and not people and not any(live.values()):
            rows.append(Text(_("No results."), size=18, color=theme.SUBTLE_FG))
        return VScroll(Column(rows, pad=chrome.CONTENT_PAD, gap=12,
                              align="stretch"), id="search", flex=1,
                       offset=self.parked_scroll("search"))
