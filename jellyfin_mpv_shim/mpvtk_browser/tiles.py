"""Tiles: rendering, art, and the tile context menu.

Poster fetch/decode plumbing, the tile / row / grid builders, the detail
banner compositor, the track list, and the right-click menu on a tile.

State on ``self``: ``_posters``, ``_requested`` and ``_img_retry`` (the
image cache — ``_image_done`` runs on a pool thread and writes then
``invalidate()``s), the ``_downloaded*`` sets behind the tile badge, and
``_menu`` (the open context menu, loop thread only).
"""

import logging


from ..i18n import _
from ..mpvtk.widgets import Menu
from . import components
from .repository import PLAYABLE_TYPES, PLAYLIST_SUPPORTED_TYPES

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

    def _track_list(self, *a, **k):
        return self.tiles.track_list(*a, **k)

    def _banner_box(self, *a, **k):
        return self.tiles.banner_box(*a, **k)

    def _is_downloaded(self, item):
        return self.tiles.is_downloaded(item)

    def _cols(self, *a, **k):
        return self.tiles.cols(*a, **k)

    def _tile(self, *a, **k):
        return self.tiles._tile(*a, **k)

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

    #: Live types get their own entries: a channel is watched rather than
    #: played into a queue, and a program is not itself playable at all.
    MENU_LIVE = {"TvChannel", "Program"}

    #: Containers the hover play chip offers, on top of MENU_PLAYABLE and
    #: MENU_LIVE: things whose contents are a queue in the order the grid is
    #: already showing them.
    #:
    #: **A LIBRARY IS NOT ONE.** A CollectionFolder or a UserView can answer
    #: Play All perfectly well -- the grid header offers exactly that once
    #: you are inside -- and it is still the wrong thing to put under the
    #: pointer on the home screen. Those tiles are the doors to the app: the
    #: gesture people make on them is "take me in", made quickly and often,
    #: and a play button there is a 1300-film queue one slip away from
    #: whatever they were actually going to do. Deciding to play a whole
    #: library should cost the click that gets you in first.
    CHIP_CONTAINERS = {"BoxSet", "Folder", "PhotoAlbum"}

    def _tile_playable(self, item):
        """Whether a hovered tile gets a play chip. Cheap and pure: this runs
        for every tile of every strip that is built."""
        t = item.get("Type")
        if t in self.MENU_LIVE or t in self.MENU_PLAYABLE:
            # Photo is deliberately absent from MENU_PLAYABLE, and should
            # stay absent here: clicking the picture already shows it.
            return True
        # CollectionType is what makes a folder a library -- a plain Folder
        # inside one has none -- so this is the test for "is this a door".
        return t in self.CHIP_CONTAINERS and not item.get("CollectionType")

    def _play_tile(self, item):
        """What the hover chip does. The tile's own click still opens the
        page -- these are different questions for anything with contents."""
        server = self.route.get("server") or self.server
        t = item.get("Type")
        if t == "Series":
            # Next Up, not the whole series from episode one: it is what
            # jellyfin-web's overlay button does on a series card, and the
            # only reading of "play this show" that does not throw away
            # where you had got to.
            self._actions.play_next_up(item.get("Id"), server)
            return
        if t in self.CHIP_CONTAINERS:
            self._play_container(item, server)
            return
        self._menu_play(item, server)

    def _play_container(self, item, server):
        """A collection, a folder or a library: its contents, in the order
        the grid shows them."""
        source = self.source
        parent = item.get("Id")
        ctype = item.get("CollectionType")
        ep = self._epoch

        def work():
            return source.get_play_all_ids(server, parent,
                                           collection_type=ctype)

        def done(ids):
            if ids:
                # pause_stills=False for the same reason Play All has it: the
                # gesture means "run it", and a queue that opens on a photo
                # would otherwise sit paused on frame one.
                self._actions.play_list(ids, server, 0, pause_stills=False)
            else:
                self.set_status(_("There is nothing here to play."))

        self.run_async(work, done, ep)

    def _live_menu_entries(self, item):
        """Menu for a channel or a guide entry.

        The guide's context menu in jellyfin-web, which is where recording a
        programme from a listing actually happens — without it the only way
        to set a timer is to open each programme in turn.
        """
        from . import live_tv

        out = []
        if item.get("Type") == "TvChannel":
            out.append((_("Watch"), "play_arrow", "play"))
            return out
        if item.get("ChannelId"):
            out.append((_("Watch Channel"), "play_arrow", "play"))
        if not self._actions.can_record():
            return out
        # single_timer_state, not timer_state: the two answer different
        # questions and a showing covered by a series rule answers yes to
        # both. See live_tv.single_timer_state.
        single = live_tv.single_timer_state(item)
        if single:
            out.append((_("Stop Recording") if single == "recording"
                        else _("Do Not Record"), "cancel", "unrecord"))
        else:
            out.append((_("Record"), "fiber_manual_record", "record"))
        if item.get("IsSeries"):
            if item.get("SeriesTimerId"):
                out.append((_("Cancel Series"), "cancel", "unrecordseries"))
            else:
                out.append((_("Record Series"), "fiber_smart_record",
                            "recordseries"))
        return out

    def _tile_menu_entries(self, item):
        """``[(label, icon, action-key)]`` for this item's type."""
        t = item.get("Type")
        ud = item.get("UserData") or {}
        watched = components.is_watched(item)
        fav = bool(ud.get("IsFavorite"))
        out = []
        if t in self.MENU_LIVE:
            out = self._live_menu_entries(item)
            if t == "TvChannel":
                # Favourites float to the top of the guide and the channel
                # list (see live_tv.channel_sort_kwargs), so this is the one
                # piece of user data a channel has that does anything.
                out.append((_("Remove from Favorites") if fav
                            else _("Add to Favorites"), "favorite",
                            "favorite"))
            return out
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
        elif action in ("record", "recordseries", "unrecord",
                        "unrecordseries"):
            self._close_menu()
            self._live_menu_action(action, item, server)
            return
        self._close_menu()

    def _live_menu_action(self, action, item, server):
        """Record / cancel from a tile or guide cell.

        Followed by a reload of the screen rather than an optimistic flip:
        the DTO on the tile has no timer id until the server issues one, so
        there is nothing to flip to (see ItemActions).
        """
        route = self.route

        def done():
            self._reload_route(route)

        if action == "record":
            self._actions.schedule_recording(item, server, on_done=done)
        elif action == "recordseries":
            self._actions.schedule_recording(item, server, series=True,
                                             on_done=done)
        elif action == "unrecord":
            self._actions.cancel_timer(item.get("TimerId"), server,
                                       on_done=done)
        else:
            self._actions.cancel_series_timer(item.get("SeriesTimerId"),
                                              server, on_done=done)

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
        if t in self.MENU_LIVE:
            # What you watch is the channel, never the guide entry — the
            # generic path below would resolve a Program to its own id and
            # try to play a listing.
            self._play_list([item.get("ChannelId") or item.get("Id")], server)
            return
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





