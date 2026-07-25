"""Tiles: rendering, art, and the tile context menu.

Poster fetch/decode plumbing, the tile / row / grid builders, the detail
banner compositor, the track list, and the right-click menu on a tile.

State on ``self``: ``_posters``, ``_requested`` and ``_img_retry`` (the
image cache — ``_image_done`` runs on a pool thread and writes then
``invalidate()``s), the ``_downloaded*`` sets behind the tile badge, and
``_menu`` (the open context menu, loop thread only).
"""

import logging

import time

from ..i18n import _
from ..mpvtk.scaling import px, raster
from ..mpvtk.widgets import (
    Box,
    Column,
    HScroll,
    Icon,
    Image,
    ImageMap,
    Menu,
    Row,
    Spacer,
    Stack,
    Table,
    Text,
    virtual_window,
)
from . import components, theme
from .repository import PLAYABLE_TYPES, PLAYLIST_SUPPORTED_TYPES
from .strips import Tile
from .thumbnails import make_key

log = logging.getLogger("mpvtk_browser.tiles")


class TilesMixin:
    """The tile context menu, plus forwarders to the renderer.

    Tile/row/grid CONSTRUCTION moved to tile_renderer.TileRenderer (step 6b).
    What is left needs route, navigate, run_async and the gateway -- it is
    page work, not rendering. The forwarders below stay until every caller is
    a Page reaching the renderer through its context.
    """

    def _tile_row(self, *a, **k):
        return self.tiles.tile_row(*a, **k)

    def _grid_of(self, *a, **k):
        return self.tiles.grid_of(*a, **k)

    def _track_list(self, *a, **k):
        return self.tiles.track_list(*a, **k)

    def _art_cell(self, *a, **k):
        return self.tiles.art_cell(*a, **k)

    def _backdrop_node(self, *a, **k):
        return self.tiles.backdrop_node(*a, **k)

    def _banner_box(self, *a, **k):
        return self.tiles.banner_box(*a, **k)

    def _is_downloaded(self, item):
        return self.tiles.is_downloaded(item)

    def _header_offset(self, *a, **k):
        return self.tiles.header_offset(*a, **k)

    def _cols(self, *a, **k):
        return self.tiles.cols(*a, **k)

    def _tile(self, *a, **k):
        return self.tiles._tile(*a, **k)

    def _poster_for(self, *a, **k):
        return self.tiles.poster_for(*a, **k)


    # -------------------------------------------------------- tile helpers

    # A thumbnail fetch that fails transiently is retried on a later
    # repaint, but not immediately: a server that is down or slow would
    # otherwise get a fresh burst on every scroll frame. Attempts are
    # capped so a permanently broken URL settles instead of retrying for
    # the life of the session.












    # ------------------------------------------------------ tile context menu

    def _open_tile_menu(self, item, x, y):
        # Nothing on offer for this type (a cast member): no menu at all,
        # rather than an empty one.
        if not self._tile_menu_entries(item):
            return
        self._menu = {"item": item,
                      "server": self.route.get("server") or self.server,
                      "x": x, "y": y}
        self.invalidate()

    def _close_menu(self):
        self._menu = None
        self.invalidate()

    # Types the tile menu offers each action for. Every entry used to be
    # shown for every item, so right-clicking a cast member offered to
    # play, download and mark a Person watched.
    MENU_PLAYABLE = PLAYABLE_TYPES | {"Audio", "MusicAlbum", "MusicArtist",
                                      "MusicGenre", "Series", "Season",
                                      "Playlist"}

    MENU_WATCHED = PLAYABLE_TYPES | {"Series", "Season"}

    # MENU_PLAYABLE minus MusicGenre: a genre is not a library item, so
    # favoriting one posts a non-favoritable id and the server rejects it.
    # (The old `MENU_PLAYABLE | {"MusicAlbum", "MusicArtist"}` read as
    # widening but was a no-op — both names were already in the set.)
    MENU_FAVORITE = MENU_PLAYABLE - {"MusicGenre"}

    MENU_ADD_TO = PLAYABLE_TYPES | {"Audio", "MusicAlbum", "MusicArtist",
                                    "MusicGenre", "Series", "Season"}

    MENU_DOWNLOAD = PLAYABLE_TYPES | {"Audio", "Series", "Season", "Playlist"}

    def _tile_menu_entries(self, item):
        """``[(label, icon, action-key)]`` for this item's type."""
        t = item.get("Type")
        ud = item.get("UserData") or {}
        watched = components.is_watched(item)
        fav = bool(ud.get("IsFavorite"))
        out = []
        if t in self.MENU_PLAYABLE:
            out.append((_("Play"), "play_arrow", "play"))
            out.append((_("Add to Queue"), "playlist_add", "queue"))
        if t in self.MENU_WATCHED:
            out.append((_("Mark Unwatched") if watched
                        else _("Mark Watched"), "check", "watched"))
        if t in self.MENU_FAVORITE:
            out.append((_("Remove from Favorites") if fav
                        else _("Add to Favorites"), "favorite", "favorite"))
        if t in self.MENU_ADD_TO and not self._offline and self._edit_apis():
            out.append((_("Add to Playlist"), "queue_music", "addto"))
        if t in self.MENU_DOWNLOAD and not self._offline:
            out.append((_("Download"), "file_download", "download"))
        # Only inside a playlist, and only for an entry that carries its
        # PlaylistItemId — removal is by entry, not by item id (the same
        # item can appear twice).
        if (self.route.get("kind") == "playlist"
                and item.get("PlaylistItemId") and not self._offline
                and self._edit_apis()):
            out.append((_("Remove from Playlist"), "delete", "unplaylist"))
        if (self.route.get("parent_type") == "BoxSet"
                and item.get("Id") and not self._offline
                and self._edit_apis()):
            out.append((_("Remove from Collection"), "delete", "uncollect"))
        return out

    def _edit_apis(self):
        return self._actions.can_edit()

    def _tile_menu_node(self):
        m = self._menu
        entries = self._tile_menu_entries(m["item"])
        if not entries:
            return None
        return Menu("tilemenu", [e[0] for e in entries], m["x"], m["y"],
                    icons=[e[1] for e in entries],
                    on_select=self._menu_action, on_dismiss=self._close_menu)

    def _menu_action(self, index, value):
        m = self._menu
        if m is None:
            return
        item, server = m["item"], m["server"]
        entries = self._tile_menu_entries(item)
        if not 0 <= index < len(entries):
            return self._close_menu()
        action = entries[index][2]
        if action == "play":
            self._menu_play(item, server)
        elif action == "queue":
            self._menu_queue(item, server)
        elif action == "watched":
            self._act_watched(item, server)
        elif action == "favorite":
            self._act_favorite(item, server)
        elif action == "addto":
            self._close_menu()
            self._open_add_to(item)
            return
        elif action == "download":
            self._close_menu()
            self._open_download(item)
            return
        elif action == "unplaylist":
            self._close_menu()
            self._remove_from_playlist(item)
            return
        elif action == "uncollect":
            self._close_menu()
            self._remove_from_collection(item)
            return
        self._close_menu()

    def _remove_from_playlist(self, item):
        entry = item.get("PlaylistItemId")
        pid = self.route.get("item_id")
        server = self.route.get("server") or self.server
        if not (entry and pid):
            return
        self._confirm(
            _("Remove %s from this playlist?") % item.get("Name", ""),
            lambda: self._do_remove_from_playlist(server, pid, entry),
            title=_("Remove from Playlist"), yes=_("Remove"))

    def _remove_from_collection(self, item):
        cid = self.route.get("parent_id")
        server = self.route.get("server") or self.server
        iid = item.get("Id")
        if not (cid and iid):
            return
        self._confirm(
            _("Remove %s from this collection?") % item.get("Name", ""),
            lambda: self._do_remove_from_collection(server, cid, iid),
            title=_("Remove from Collection"), yes=_("Remove"))

    def _do_remove_from_collection(self, server, collection_id, item_id):
        route = self.route
        ep = self._epoch

        def work():
            self.controller.collection_remove(server, collection_id,
                                              [item_id])

        def done(_ok):
            # Re-read: the grid still lists what was just removed.
            route.pop("_items", None)
            route.pop("_loading", None)
            self._load_route(route)

        def failed(_exc):
            self.set_status(_("The change could not be applied."))
        self.run_async(work, done, ep, on_error=failed)

    def _do_remove_from_playlist(self, server, playlist_id, entry_id):
        ep = self._epoch

        def work():
            self.controller.playlist_remove(server, playlist_id, [entry_id])
            return self.source.get_playlist_items(server, playlist_id)

        def done(items):
            self.route["_data"] = items
            self.invalidate()

        def failed(_exc):
            self.set_status(_("The change could not be applied."))
        self.run_async(work, done, ep, on_error=failed)

    def _menu_queue(self, item, server):
        """Append to the playing queue. A music container is resolved to its
        tracks first — queueing the container id itself is meaningless."""
        ep = self._epoch
        parent = self.route.get("parent_id")

        def work():
            return self._resolve_play_ids(item, server, parent)

        def done(ids):
            if ids:
                self._queue_items(ids, server)
            else:
                # A container that resolved to nothing: say so rather than
                # having the menu entry appear to do nothing at all.
                self.set_status(_("There is nothing here to queue."))
                self.invalidate()

        def failed(_exc):
            self.set_status(_("Those items could not be added to the queue."))
            self.invalidate()

        self.run_async(work, done, ep, on_error=failed)

    def _resolve_play_ids(self, item, server, parent_id=None):
        """The item ids "Play"/"Add to Queue" should act on.

        A music container (album/artist/playlist/series) is not itself a
        playable item — queueing or playing its own id does nothing, which
        is why Play on an album tile used to just navigate. Runs off the
        loop thread: these hit the server.

        ``parent_id`` (a genre's library) must be captured by the CALLER on
        the loop thread. Reading self.route here raced navigation: a genre
        could resolve against whatever page the user had moved on to."""
        t, iid = item.get("Type"), item.get("Id")
        if not iid:
            return []
        try:
            if t == "MusicAlbum":
                return [i.get("Id")
                        for i in self.source.get_album_tracks(server, iid)]
            if t == "MusicArtist":
                return [i.get("Id")
                        for i in self.source.get_artist_songs(server, iid)]
            if t == "Playlist":
                return [i.get("Id") for i in
                        self.source.get_playlist_items(server, iid)
                        if i.get("Type") in PLAYLIST_SUPPORTED_TYPES]
            if t == "MusicGenre":
                return [i.get("Id") for i in self.source.get_genre_songs(
                    server, parent_id, iid)]
            if t in ("Series", "Season"):
                return [i.get("Id") for i in
                        self.source.get_series_queue(server, iid)]
        except Exception:
            log.warning("could not resolve %s for playback", t, exc_info=True)
            return []
        return [iid]

    def _menu_play(self, item, server):
        t = item.get("Type")
        if t == "Audio":
            self._play_list([item.get("Id")], server, audio=True)
            return
        if t in PLAYABLE_TYPES:
            self._play(item, server)
            return
        # A container: resolve it to its items and play those, rather than
        # navigating (a "Play" that browses instead is just a lie).
        ep = self._epoch
        audio = t in ("MusicAlbum", "MusicArtist", "MusicGenre")
        parent = self.route.get("parent_id")

        def work():
            return self._resolve_play_ids(item, server, parent)

        def done(ids):
            if ids:
                self._play_list(ids, server, 0, audio=audio)
            else:
                self._open_item(item)
        self.run_async(work, done, ep)







    # ---------------------------------------------------- music / playlists





