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

import logging

from ...books import AUDIOBOOK_TYPE, BOOK_TYPE
from ...i18n import _
from ...mpvtk.widgets import Column, Text, VScroll
from .. import theme
from ..components import chrome
from .base import Page

log = logging.getLogger("mpvtk_browser.pages.search")

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

#: Sentinel geom: shape this row from the artwork it actually got, via
#: ``TileRenderer.auto_geom``. Only the two book rows use it -- every other
#: row here has one true shape, and inferring theirs would let a single
#: oddly-tagged result restyle the whole carousel.
AUTO_SHAPE = object()

#: The two entries in the section order that are not "filter the items by
#: type into a carousel": People come from their own request, and Songs are
#: a table. Objects rather than strings so they cannot collide with a row
#: label, which is translated and therefore not a constant.
PEOPLE_ROW = object()
ARTISTS_ROW = object()
SONGS_ROW = object()


class SearchPage(Page):
    kind = "search"

    def load(self, epoch):
        srv = self.route.get("server") or self.ctx.server
        term = self.route.get("term", "")
        source = self.ctx.source
        route = self.route

        def work():
            if not term:
                return {"items": [], "people": [], "artists": [], "live": {}}
            items = source.search(srv, term)
            people = []
            try:
                people = source.search_people(srv, term)
            except Exception:
                pass
            # Artists are their own request, like people and for the same
            # reason: /Items does not answer with them reliably (it returns
            # fewer than /Artists here and none at all on some servers), and
            # a featured artist has no MusicArtist item to be found as.
            # getattr'd because a source may predate the method.
            artists = []
            search_artists = getattr(source, "search_artists", None)
            if search_artists is not None:
                try:
                    artists = search_artists(srv, term)
                except Exception:
                    log.debug("artist search failed", exc_info=True)
            # Live TV is searched separately — its items are not in the
            # /Search/Hints media types — and only where there is a tuner,
            # so the overwhelming majority of users pay nothing for it. Both
            # halves are getattr'd: the offline source has neither.
            live = {}
            if getattr(source, "has_live_tv", lambda _s: False)(srv):
                search_live = getattr(source, "search_live_tv", None)
                if search_live is not None:
                    live = search_live(srv, term) or {}
            return {"items": items, "people": people, "artists": artists,
                    "live": live}

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
        artists = data.get("artists") or []
        rows = [Text(_('Results for "%s"') % term, size="heading", bold=True)]
        # Searching is a keyboard gesture even from a remote (the search
        # button puts the cursor in the box), so the results land focused
        # on the first of them — otherwise submitting leaves focus in the
        # box, where the arrow keys still move the caret. Whichever row
        # comes first owns it; `first` is claimed by the first row built.
        first = [True]

        def claim():
            got, first[0] = first[0], False
            return got

        # Group by type, each with its natural tile shape (like the Tk browser).
        # The last column is ``inherit``: whether a tile may fall back to its
        # series' artwork. Episodes say no -- a result row of episodes is
        # about the episodes, and inheriting draws one show's thumb over
        # several of them, which is the same thing that made a season grid
        # useless. jellyfin-web gets there differently (its search Episodes
        # row sets no preferThumb at all, so it lands on the episode's own
        # Primary); asking for a *landscape* image of the episode first is
        # the same intent in a shape that fits the tile.
        #
        # **The order is jellyfin-web's** (SEARCH_SECTIONS_SORT_ORDER): what
        # you searched for comes first and the people who made it come after.
        # People used to lead, which put a row of faces above the film whose
        # title had just been typed -- and, because the first row built takes
        # focus, left the remote's first keypress on the cast. Web's People
        # sits after Episodes, and its Artists after People; Videos falls
        # below Songs. Studios, Playlists, Books and Photos are in that list
        # too and are simply rows this client does not draw.
        groups = [
            (_("Movies"), ("Movie",), art.geom, "Primary", True),
            (_("Shows"), ("Series",), art.geom, "Primary", True),
            (_("Episodes"), ("Episode",), art.geom_wide, "Thumb", False),
            (PEOPLE_ROW, (), art.geom, "Primary", True),
            (ARTISTS_ROW, (), art.geom_square, "Primary", True),
            (_("Albums"), ("MusicAlbum",), art.geom_square, "Primary", True),
            (SONGS_ROW, (), None, None, None),
            (_("Videos"), ("Video", "MusicVideo"), art.geom_wide, "Primary",
             True),
            # Web's order puts these last but for Collections, and it draws
            # them AutoOverflow -- shape inferred from the artwork rather
            # than from the type. That is the honest rule for these two:
            # an audiobook wears whatever the ripper embedded (square art
            # from the audio file, or the book's portrait cover), and a
            # comic is a different shape from a novel. AUTO_SHAPE asks
            # auto_geom, which is the same call the Live TV rows make.
            (_("Audiobooks"), (AUDIOBOOK_TYPE,), AUTO_SHAPE, None, True),
            (_("Books"), (BOOK_TYPE,), AUTO_SHAPE, None, True),
        ]
        used = set()
        for label, types_, geom, itype, inherit in groups:
            if label is PEOPLE_ROW:
                if people:
                    rows.append(tiles.tile_row(
                        _("People"), people[:ROW_MAX], "search-people",
                        geom=art.geom, autofocus_first=claim()))
                continue
            if label is ARTISTS_ROW:
                # The dedicated request first, then any MusicArtist items the
                # search happened to return. Both directions have been seen:
                # here /Artists finds more than the item query, and on other
                # servers the item query finds none. Marking the type used
                # either way keeps a stray one out of the Other row, which is
                # where it landed the moment artists left SEARCH_TYPES.
                used.add("MusicArtist")
                row = artists or [it for it in items
                                  if it.get("Type") == "MusicArtist"]
                if row:
                    rows.append(tiles.tile_row(
                        _("Artists"), row[:ROW_MAX], "search-Artists",
                        geom=art.geom_square, autofocus_first=claim()))
                continue
            if label is SONGS_ROW:
                rows.extend(self._songs_row(items, route, tiles))
                continue
            group = [it for it in items if it.get("Type") in types_][:ROW_MAX]
            if group:
                used.update(types_)
                row_geom, row_itype = geom, itype
                if geom is AUTO_SHAPE:
                    row_geom, row_itype = tiles.auto_geom(
                        group, default=art.geom, default_type="Primary")
                rows.append(tiles.tile_row(
                    label, group, "search-" + label, geom=row_geom,
                    image_type=row_itype, inherit=inherit,
                    autofocus_first=claim()))
        # Programs before Channels, which is the rest of web's order: what
        # is on now is a result, a channel is a place to go and look.
        live = data.get("live") or {}
        if live.get("programs"):
            rows.append(tiles.tile_row(_("On TV"), live["programs"],
                                       "search-programs", geom=art.geom_wide,
                                       image_type="Thumb"))
        if live.get("channels"):
            rows.append(tiles.tile_row(_("Channels"), live["channels"],
                                       "search-channels",
                                       geom=art.geom_square))
        other = [it for it in items
                 if it.get("Type") not in used
                 and it.get("Type") != "Audio"][:ROW_MAX]
        if other:
            rows.append(tiles.tile_row(_("Other"), other, "search-other"))
        if not items and not people and not artists and not any(live.values()):
            rows.append(Text(_("No results."), size="large", color=theme.SUBTLE_FG))
        return VScroll(Column(rows, pad=chrome.CONTENT_PAD, gap=12,
                              align="stretch"), id="search", flex=1,
                       offset=self.parked_scroll("search"))

    def _songs_row(self, items, route, tiles):
        """The Songs heading and its table, or an empty list.

        A method only because the section order is a list the render loop
        walks now, and songs are the one entry in it that is a table rather
        than a carousel. It never claims focus: a table row is reachable by
        arrow keys from the carousel above it, and a search that matched a
        song and a film should land on the film.
        """
        rows = []
        songs = [it for it in items if it.get("Type") == "Audio"][:ROW_MAX]
        if songs:
            server = route.get("server") or self.ctx.server
            ids = [s.get("Id") for s in songs]
            rows.append(Text(_("Songs"), size="heading", bold=True))
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
        return rows
