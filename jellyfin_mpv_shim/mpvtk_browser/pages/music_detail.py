"""One album, one artist, one genre — the three "container of tracks" screens.

All three subclass ``MusicPage`` for the action bar and header block, which
is the whole reason that base exists.
"""

from ...i18n import _
from ...mpvtk.widgets import Column, Row, Spacer, Text, VScroll
from ..components import chrome
from ..tile_renderer import GRID_GAP
from .music import MusicPage


class AlbumPage(MusicPage):
    kind = "album"

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server
        iid = route["item_id"]

        def work():
            return {"item": source.get_item(srv, iid),
                    "tracks": source.get_album_tracks(srv, iid)}

        self.route_async(work, lambda d: route.__setitem__("_data", d), epoch)

    def render(self, size):
        art = self.ctx.art
        route = self.route
        data = route.get("_data")
        if data is None:
            return chrome.busy()
        item = data.get("item") or {}
        tracks = data.get("tracks") or []
        server = route.get("server") or self.ctx.server
        ids = [t.get("Id") for t in tracks]
        header = Row([
            art.tiles.art_cell(item, size=132),
            Column(self.header_text(item, tracks, size[0]) + [
                self.action_bar(server, ids, route["item_id"], "album",
                                items=tracks),
            ], gap=8, flex=1, align="stretch"),
        ], gap=16, align="start")
        body = art.tiles.track_list(
            tracks, "trk",
            lambda i: self.ctx.actions.play_list(ids, server, i, audio=True),
            scroll_id="album", head_h=110, menu=True)
        # on_scroll, like every other virtualized list. The offset lookup
        # prefers the renderer's live property, so this looked fine — but on
        # mpv < 0.36 that property does not exist and the throttled on_scroll
        # copy is the ONLY source. Without it the window stayed at offset 0
        # and everything past the first screenful drew blank.
        return VScroll(Column([header, body], pad=chrome.CONTENT_PAD, gap=12,
                              align="stretch"),
                       id="album", flex=1,
                       on_scroll=lambda off, mx: art.scroll.on_scroll(
                           "album", off, mx))


class ArtistPage(MusicPage):
    kind = "artist"

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server
        iid = route["item_id"]

        def work():
            songs: list = []
            similar: list = []
            try:
                songs = source.get_artist_songs(srv, iid)
            except Exception:
                pass
            try:
                similar = source.get_similar(srv, iid) or []
            except Exception:
                pass   # offline / older server: just no row
            item: dict = {}
            try:
                item = source.get_item(srv, iid) or {}
            except Exception:
                pass   # older server / offline: header degrades to the title
            return {"item": item,
                    "albums": source.get_artist_albums(srv, iid),
                    "songs": songs, "similar": similar}

        self.route_async(work, lambda d: route.__setitem__("_data", d), epoch)

    def render(self, size):
        art = self.ctx.art
        route = self.route
        data = route.get("_data")
        if data is None:
            return chrome.busy()
        albums = data.get("albums") or []
        songs = data.get("songs") or []
        server = route.get("server") or self.ctx.server
        ids = [s.get("Id") for s in songs]
        item = data.get("item") or {}
        rows: list = [Row([
            art.tiles.art_cell(item, size=132) if item else Spacer(w=0),
            Column(self.header_text(item, songs, size[0]) + [
                self.action_bar(server, ids, route["item_id"], "art",
                                items=songs),
            ], gap=8, flex=1, align="stretch"),
        ], gap=16, align="start")]
        if albums:
            rows.append(Text(_("Albums"), size=20, bold=True))
        rows += art.tiles.grid_of(albums, "artist", size,
                                  geom=art.geom_square,
                                  scroll_id="artist", head_h=110)
        similar = data.get("similar") or []
        if similar:
            rows.append(Spacer(h=8))
            rows.append(art.tiles.tile_row(_("Similar Artists"), similar,
                                           "artist-similar",
                                           geom=art.geom_square))
        return VScroll(Column(rows, pad=chrome.CONTENT_PAD, gap=GRID_GAP),
                       id="artist", flex=1,
                       on_scroll=lambda off, mx: art.scroll.on_scroll(
                           "artist", off, mx))


class MusicGenrePage(MusicPage):
    kind = "music_genre"

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server

        def work():
            songs = []
            try:
                songs = source.get_genre_songs(
                    srv, route.get("parent_id"), route["item_id"])
            except Exception:
                pass
            albums, total = source.get_genre_albums(
                srv, route.get("parent_id"), route["item_id"])
            return {"albums": albums, "songs": songs, "total": total}

        self.route_async(work, lambda d: route.__setitem__("_data", d), epoch)

    def render(self, size):
        art = self.ctx.art
        route = self.route
        data = route.get("_data")
        if data is None:
            return chrome.busy()
        albums = data.get("albums") or []
        songs = data.get("songs") or []
        server = route.get("server") or self.ctx.server
        ids = [s.get("Id") for s in songs]
        rows: list = [Text(route.get("title", ""), size=26, bold=True),
                      Spacer(h=4),
                      self.action_bar(server, ids, route["item_id"], "gen")]
        rows += art.tiles.grid_of(albums, "mgenre", size,
                                  geom=art.geom_square,
                                  scroll_id="mgenre", head_h=110)
        return VScroll(Column(rows, pad=chrome.CONTENT_PAD, gap=GRID_GAP),
                       id="mgenre", flex=1,
                       on_scroll=lambda off, mx: art.scroll.on_scroll(
                           "mgenre", off, mx,
                           lambda o, m: self._on_scroll_end(o, m)))

    def _on_scroll_end(self, offset, maximum):
        """Page a genre's albums. It rendered one 100-album page with no
        scroll handler, so a large genre simply stopped there."""
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server
        parent = route.get("parent_id")
        gid = route["item_id"]

        def put(r, items, total):
            data = r.get("_data") or {}
            data["albums"], data["total"] = items, total

        self.ctx.art.pages.more(
            route, offset, maximum,
            lambda r: ((r.get("_data") or {}).get("albums") or [],
                       (r.get("_data") or {}).get("total") or 0),
            put,
            lambda start: source.get_genre_albums(srv, parent, gid,
                                                  start_index=start))
