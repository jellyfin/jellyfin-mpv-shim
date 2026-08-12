"""A playlist: a track list when it holds music, a tile grid otherwise."""

from ...i18n import _
from ...mpvtk.widgets import Column, Row, Spacer, Text, VScroll
from .. import theme
from ..components import chrome, controls, detail as detail_components
from ..repository import PLAYLIST_SUPPORTED_TYPES
from ..tile_renderer import GRID_GAP
from .base import Page


class PlaylistPage(Page):
    kind = "playlist"

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server
        iid = route["item_id"]

        self.route_async(lambda: source.get_playlist_items(srv, iid),
                         lambda d: route.__setitem__("_data", d), epoch)

    def render(self, size):
        art = self.ctx.art
        actions = self.ctx.actions
        route = self.route
        data = route.get("_data")
        if data is None:
            return chrome.busy()
        server = route.get("server") or self.ctx.server
        pid = route["item_id"]
        raw = list(data)
        # A playlist's declared type and its contents can diverge, so filter
        # by what's actually playable rather than trusting the container.
        items = [i for i in raw if i.get("Type") in PLAYLIST_SUPPORTED_TYPES]
        ids = [i.get("Id") for i in items]
        # any(), like Tk: a playlist with any music in it reads better as a
        # track list than as a grid of mismatched artwork.
        audio = any(i.get("Type") == "Audio" for i in items)
        pl_item = {"Id": pid, "Type": "Playlist",
                   "Name": route.get("title", "")}
        header = Row([
            Text(route.get("title", ""), size="hero", bold=True),
            Spacer(),
            # Play All / Shuffle only when there is something to play — they
            # rendered above the empty check, so an empty playlist offered
            # two buttons that called play_list with no ids and returned.
            controls.action_btn("play_arrow", _("Play All"), "pl-play",
                                lambda: actions.play_list(ids, server, 0,
                                                          audio=audio,
                                                          items=items),
                                primary=True) if ids else None,
            controls.action_btn("shuffle", _("Shuffle"), "pl-shuffle",
                                lambda: actions.play_shuffle(ids, server,
                                                             audio=audio))
            if ids else None,
            detail_components.download_button(actions, art.tiles, pl_item,
                                              server, "pl"),
            controls.action_btn("edit", _("Edit"), "pl-edit",
                                lambda: self.ctx.nav.navigate({
                                    "kind": "playlist_edit", "server": server,
                                    "item_id": pid,
                                    "title": route.get("title", "")}))
            # Offline (or on an apiclient that can't edit) every control on
            # that page fails; don't offer the door.
            if not actions.offline and actions.can_edit() else None,
        ], align="center", gap=10)
        if not items:
            body = [Text(
                _("This playlist is empty.") if not raw else
                _("This playlist has no supported media types."),
                size="large", color=theme.SUBTLE_FG)]
        elif audio:
            # Music playlists read as a track list, like the Tk browser —
            # a wall of identical album covers tells you nothing. Per-track
            # art earns its column here though: albums differ per row.
            body = [art.tiles.track_list(
                items, "pl",
                lambda i: actions.play_list(ids, server, i, audio=True,
                                            items=items),
                art=True, scroll_id="playlist", head_h=70, menu=True)]
        else:
            # `items`, not `data`: unsupported entries were rendering as
            # tiles whose click did something unrelated. And a click plays
            # the PLAYLIST from that point — going through the tile's normal
            # open meant Play on the detail page queued the item's series
            # instead, silently abandoning the playlist the user was in.
            # Shaped by its own artwork, like every other grid in the app
            # (and like jellyfin-web's cardBuilder). This was the one that
            # was not: a playlist can hold anything, and a playlist of
            # episodes -- whose art is 16:9 stills -- was drawn in 2:3
            # poster tiles, so the server was asked to fill-crop every
            # still into a portrait frame and most of each picture went.
            # A playlist of films still comes out as posters; that is the
            # same rule reaching a different answer.
            geom, image_type = art.tiles.auto_geom(items)
            body = art.tiles.grid_of(
                items, "pl", size, geom=geom, image_type=image_type,
                # inherit=False, for the reason the season listing gives:
                # this is a list of *entries*, and each cell has to be the
                # thing you would be playing. The Thumb chain otherwise
                # borrows the series' thumb or backdrop for an episode that
                # has no still of its own, so a playlist built out of one
                # show draws the same series artwork in every cell and stops
                # distinguishing anything. Borrowing is right for a Continue
                # Watching card, which is a pointer back to the show; a
                # playlist entry is not one.
                inherit=False,
                scroll_id="playlist", head_h=70,
                on_click=lambda it: actions.play_list(
                    ids, server, items.index(it), audio=False, items=items))
        return VScroll(Column([header, Spacer(h=2)] + body,
                              pad=chrome.CONTENT_PAD, gap=GRID_GAP,
                              align="stretch"),
                       id="playlist", flex=1,
                       offset=self.parked_scroll("playlist"),
                       on_scroll=lambda off, mx: art.scroll.on_scroll(
                           "playlist", off, mx))
