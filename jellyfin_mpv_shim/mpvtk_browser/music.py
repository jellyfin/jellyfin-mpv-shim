"""Music browsing and the now-playing bar.

The music tabs (albums / artists / songs / genres / playlists), the album,
artist, genre and playlist views, and the persistent audio bar.

State on ``self``: ``_now_playing`` (the latest playstate snapshot, written
from a foreign thread by core's ``on_playstate``) and ``_np_thread`` (the
1s ticker that keeps the bar's clock moving). Paging
state lives in the route dict.
"""

from ..i18n import _
from ..mpvtk.widgets import (
    Box,
    Button,
    Column,
    Icon,
    Row,
    Slider,
    Spacer,
    Text,
    VScroll,
)
from . import theme
from .repository import PLAYLIST_SUPPORTED_TYPES


class MusicMixin:

    # kind -> (loader, renderer) method names. Merged into
    # one dispatch table by core's _routes().
    ROUTES = {
        "album": ("_load_album", "_render_album"),
        "artist": ("_load_artist", "_render_artist"),
        "music": ("_load_music", "_render_music"),
        "music_genre": ("_load_music_genre", "_render_music_genre"),
        "playlist": ("_load_playlist", "_render_playlist"),
    }

    @staticmethod
    def _duration(item):
        secs = (item.get("RunTimeTicks") or 0) // 10000000
        return "%d:%02d" % (secs // 60, secs % 60) if secs else ""

    @staticmethod
    def _artists(item):
        return ", ".join(item.get("Artists") or item.get("AlbumArtists") or [])

    def _play_shuffle(self, ids, server, audio=True):
        import random
        ids = [i for i in ids if i]
        random.shuffle(ids)
        self._play_list(ids, server, 0, audio=audio)

    def _queue_items(self, ids, server):
        self._client_call(lambda c: c.queue_items(server, [i for i in ids if i]))

    def _instant_mix(self, seed_id, server):
        ep = self._epoch

        def work():
            return self.source.get_instant_mix(server, seed_id)

        def done(items):
            self._play_list([i.get("Id") for i in items], server, 0,
                            audio=True)
        self.run_async(work, done, ep)

    def _music_action_bar(self, server, ids, seed_id, prefix="ma", items=None):
        """Play / Shuffle / Queue / Instant Mix for a set of track ids.

        The first three are dropped when there are no ids: the artist page
        renders this bar even when the song fetch failed, and _play_list
        returns silently on an empty list, so they were dead clicks. Instant
        Mix stays — it seeds from the container, not the tracks."""
        btns = []
        if ids:
            btns += [
                self._action_btn("play_arrow", _("Play"), prefix + "-play",
                                 lambda: self._play_list(ids, server, 0,
                                                         audio=True,
                                                         items=items),
                                 primary=True),
                self._action_btn("shuffle", _("Shuffle"), prefix + "-shuffle",
                                 lambda: self._play_shuffle(ids, server)),
                self._action_btn("playlist_add", _("Add to Queue"),
                                 prefix + "-queue",
                                 lambda: self._queue_items(ids, server)),
            ]
        if seed_id:
            btns.append(self._action_btn(
                "queue_music", _("Instant Mix"), prefix + "-mix",
                lambda: self._instant_mix(seed_id, server)))
        return Row(btns, gap=8, align="center")

    def _music_tab(self, route, label, tab):
        active = route.get("_tab", "albums") == tab
        return Button(label, id="mtab-" + tab,
                      bg=theme.ACCENT if active else theme.BUTTON_BG,
                      fg=theme.ACCENT_FG if active else theme.TEXT_FG,
                      on_click=lambda: self._set_music_tab(route, tab))

    def _set_music_tab(self, route, tab):
        route["_tab"] = tab
        for k in ("_data", "_total"):
            route.pop(k, None)
        route["_loading"] = False
        # A new tab starts at the top; a stale offset would virtualize the
        # wrong window and show a screenful of blank rows.
        self._scroll_off.pop("music-grid", None)
        self._scroll_off.pop("music-songs", None)
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def _music_fetch(self, route):
        """Return a ``fetch(start_index)`` for the route's music tab, giving
        ``(items, total)``. Genres are unpaged server-side, so they report
        their own length as the total.

        **Call this on the loop thread.** It binds the server, library and
        tab *now*; reading them when the page lands would resolve against
        whichever tab the user had switched to by then.
        """
        srv = route.get("server") or self.server
        parent = route["parent_id"]
        tab = route.get("_tab", "albums")

        def fetch(start_index):
            if tab == "albumartists":
                return self.source.get_album_artists(
                    srv, parent, start_index=start_index)
            if tab == "artists":
                return self.source.get_artists(
                    srv, parent, start_index=start_index)
            if tab == "songs":
                return self.source.get_songs(
                    srv, parent, start_index=start_index)
            if tab == "genres":
                genres = self.source.get_music_genres(srv, parent)
                return (genres if start_index == 0 else []), len(genres)
            return self.source.get_music_albums(
                srv, parent, start_index=start_index)
        return fetch

    def _on_music_scroll(self, route, offset, maximum):
        """Page the current music tab in near the bottom (the Tk browser's
        _MusicGrid did this per tab; without it a library is capped at the
        first 100 albums)."""
        def put(r, items, total):
            r["_data"], r["_total"] = items, total

        self._page_more(
            route, offset, maximum,
            lambda r: (r.get("_data") or [], r.get("_total") or 0),
            put, self._music_fetch(route))

    def _render_music(self, route, size):
        tabs = Row([
            self._music_tab(route, _("Albums"), "albums"),
            self._music_tab(route, _("Album Artists"), "albumartists"),
            self._music_tab(route, _("Artists"), "artists"),
            self._music_tab(route, _("Songs"), "songs"),
            self._music_tab(route, _("Genres"), "genres"),
        ], gap=8)
        data = route.get("_data")
        if data is None:
            body = self._busy()
        elif route.get("_tab") == "songs":
            server = route.get("server") or self.server
            ids = [s.get("Id") for s in data]
            body = VScroll(Column([self._track_list(
                data, "song",
                lambda i: self._play_list(ids, server, i, audio=True),
                scroll_id="music-songs", menu=True)],
                pad=self.CONTENT_PAD, align="stretch"),
                id="music-songs", flex=1,
                on_scroll=lambda off, mx: self._on_scroll(
                    "music-songs", off, mx,
                    lambda o, m: self._on_music_scroll(route, o, m)))
        else:
            tab = route.get("_tab")
            geom = (self.geom_wide if tab == "genres"
                    else self.geom_square)
            body = VScroll(
                Column(self._grid_of(data, "music", size, geom=geom,
                                     scroll_id="music-grid"),
                       pad=self.CONTENT_PAD, gap=self.GRID_GAP),
                id="music-grid", flex=1,
                on_scroll=lambda off, mx: self._on_scroll(
                    "music-grid", off, mx,
                    lambda o, m: self._on_music_scroll(route, o, m)))
        return Column([Row([tabs], pad=12), body], flex=1, align="stretch")

    def _music_header_text(self, item, route, tracks):
        """Title / metadata / overview for an album or artist page.

        Both were a bare title: no cover, no year or genre, no Overview, and
        on the artist page no heading over the album grid either. Tk showed
        all of it, and on a music library it is most of what tells one entry
        from another."""
        out = [Text(item.get("Name") or route.get("title", ""), size=28,
                    bold=True)]
        meta = [x for x in (self._meta_line(item),
                            (_("%d tracks") % len(tracks)) if tracks else "")
                if x]
        if meta:
            out.append(Text("   ·   ".join(meta), size=15,
                            color=theme.SUBTLE_FG))
        overview = (item.get("Overview") or "").strip()
        if overview:
            out.append(self._paragraph(overview, 15,
                                       self._body_w(self._size[0])
                                       if self._size else 600))
        return out

    def _render_album(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        item = data.get("item") or {}
        tracks = data.get("tracks") or []
        server = route.get("server") or self.server
        ids = [t.get("Id") for t in tracks]
        header = Row([
            self._art_cell(item, size=132),
            Column(self._music_header_text(item, route, tracks) + [
                self._music_action_bar(server, ids, route["item_id"], "album",
                                       items=tracks),
            ], gap=8, flex=1, align="stretch"),
        ], gap=16, align="start")
        body = self._track_list(
            tracks, "trk",
            lambda i: self._play_list(ids, server, i, audio=True),
            scroll_id="album", head_h=110, menu=True)
        return VScroll(Column([header, body], pad=self.CONTENT_PAD, gap=12,
                              align="stretch"),
                       id="album", flex=1)

    def _render_artist(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        albums = data.get("albums") or []
        songs = data.get("songs") or []
        server = route.get("server") or self.server
        ids = [s.get("Id") for s in songs]
        item = data.get("item") or {}
        rows = [Row([
            self._art_cell(item, size=132) if item else Spacer(w=0),
            Column(self._music_header_text(item, route, songs) + [
                self._music_action_bar(server, ids, route["item_id"], "art",
                                       items=songs),
            ], gap=8, flex=1, align="stretch"),
        ], gap=16, align="start")]
        if albums:
            rows.append(Text(_("Albums"), size=20, bold=True))
        rows += self._grid_of(albums, "artist", size, geom=self.geom_square,
                              scroll_id="artist", head_h=110)
        similar = data.get("similar") or []
        if similar:
            rows.append(Spacer(h=8))
            rows.append(self._tile_row(_("Similar Artists"), similar,
                                       "artist-similar",
                                       geom=self.geom_square))
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=self.GRID_GAP),
                       id="artist", flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "artist", off, mx))

    def _render_music_genre(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        albums = data.get("albums") or []
        songs = data.get("songs") or []
        server = route.get("server") or self.server
        ids = [s.get("Id") for s in songs]
        rows = [Text(route.get("title", ""), size=26, bold=True),
                Spacer(h=4),
                self._music_action_bar(server, ids, route["item_id"], "gen")]
        rows += self._grid_of(albums, "mgenre", size, geom=self.geom_square,
                              scroll_id="mgenre", head_h=110)
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=self.GRID_GAP),
                       id="mgenre", flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "mgenre", off, mx,
                           lambda o, m: self._on_genre_scroll(route, o, m)))

    def _on_genre_scroll(self, route, offset, maximum):
        """Page a genre's albums. It rendered one 100-album page with no
        scroll handler, so a large genre simply stopped there."""
        srv = route.get("server") or self.server
        parent = route.get("parent_id")
        gid = route["item_id"]

        def put(r, items, total):
            data = r.get("_data") or {}
            data["albums"], data["total"] = items, total

        self._page_more(
            route, offset, maximum,
            lambda r: ((r.get("_data") or {}).get("albums") or [],
                       (r.get("_data") or {}).get("total") or 0),
            put,
            lambda start: self.source.get_genre_albums(
                srv, parent, gid, start_index=start))

    def _render_playlist(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        server = route.get("server") or self.server
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
            Text(route.get("title", ""), size=28, bold=True),
            Spacer(),
            # Play All / Shuffle only when there is something to play — they
            # rendered above the empty check, so an empty playlist offered
            # two buttons that called _play_list with no ids and returned.
            self._action_btn("play_arrow", _("Play All"), "pl-play",
                             lambda: self._play_list(ids, server, 0,
                                                     audio=audio, items=items),
                             primary=True) if ids else None,
            self._action_btn("shuffle", _("Shuffle"), "pl-shuffle",
                             lambda: self._play_shuffle(ids, server,
                                                        audio=audio))
            if ids else None,
            self._download_btn(pl_item, server, "pl"),
            self._action_btn("edit", _("Edit"), "pl-edit",
                             lambda: self.navigate({
                                 "kind": "playlist_edit", "server": server,
                                 "item_id": pid,
                                 "title": route.get("title", "")}))
            # Offline (or on an apiclient that can't edit) every control on
            # that page fails; don't offer the door.
            if not self._offline and self._edit_apis() else None,
        ], align="center", gap=10)
        if not items:
            body = [Text(
                _("This playlist is empty.") if not raw else
                _("This playlist has no supported media types."),
                size=18, color=theme.SUBTLE_FG)]
        elif audio:
            # Music playlists read as a track list, like the Tk browser —
            # a wall of identical album covers tells you nothing. Per-track
            # art earns its column here though: albums differ per row.
            body = [self._track_list(
                items, "pl",
                lambda i: self._play_list(ids, server, i, audio=True,
                                          items=items),
                art=True, scroll_id="playlist", head_h=70, menu=True)]
        else:
            # `items`, not `data`: unsupported entries were rendering as
            # tiles whose click did something unrelated. And a click plays
            # the PLAYLIST from that point — going through _open_item meant
            # Play on the detail page queued the item's series instead,
            # silently abandoning the playlist the user was in.
            body = self._grid_of(
                items, "pl", size, scroll_id="playlist", head_h=70,
                on_click=lambda it: self._play_list(
                    ids, server, items.index(it), audio=False, items=items))
        return VScroll(Column([header, Spacer(h=2)] + body,
                              pad=self.CONTENT_PAD, gap=self.GRID_GAP,
                              align="stretch"),
                       id="playlist", flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "playlist", off, mx))

    # -------------------------------------------------- now-playing bar

    @staticmethod
    def _fmt(secs):
        secs = int(secs or 0)
        return "%d:%02d" % (secs // 60, secs % 60)

    def _ctl(self, fn):
        if self.controller is not None:
            fn(self.controller)

    _REPEAT = ["none", "all", "one"]

    def _cycle_repeat(self):
        np = self._now_playing or {}
        cur = np.get("repeat", "none")
        nxt = self._REPEAT[(self._REPEAT.index(cur) + 1) % 3] \
            if cur in self._REPEAT else "all"
        np["repeat"] = nxt
        self._ctl(lambda c: c.set_repeat(nxt))
        self.invalidate()

    def _toggle_np_favorite(self):
        np = self._now_playing or {}
        np["favorite"] = not np.get("favorite")
        self._ctl(lambda c: c.toggle_favorite())
        self.invalidate()

    def _now_playing_bar(self, w):
        np = self._now_playing
        pos = np.get("position", 0) or 0
        dur = np.get("duration", 0) or 0
        pp = "play_arrow" if np.get("paused") else "pause"
        repeat = np.get("repeat", "none")

        def tbtn(icon, node_id, cb, color="eeeeee"):
            return Box([Icon(icon, 22, color=color)], id=node_id, pad=8,
                       bg=theme.BUTTON_BG, hover={"fill": theme.BUTTON_ACTIVE},
                       radius=6, align="center", direction="row", on_click=cb)

        # commit-only: dragging shouldn't spam absolute seeks mid-gesture
        seek = Slider("np-seek", value=pos, min=0, max=max(1, dur),
                      force=True, flex=1,
                      on_commit=lambda v: self._ctl(lambda c: c.seek(v)))
        title = np.get("title", "")
        sub = np.get("artist") or np.get("album") or ""
        return Row(
            [
                Column([Text(title, size=16, bold=True),
                        Text(sub, size=13, color=theme.SUBTLE_FG)],
                       gap=2, w=220),
                tbtn("skip_previous", "np-prev",
                     lambda: self._ctl(lambda c: c.prev())),
                tbtn(pp, "np-pp", lambda: self._ctl(lambda c: c.toggle_pause())),
                tbtn("skip_next", "np-next",
                     lambda: self._ctl(lambda c: c.next())),
                tbtn("stop", "np-stop", lambda: self._ctl(lambda c: c.stop())),
                Text(self._fmt(pos), size=14, w=48, color=theme.SUBTLE_FG),
                seek,
                Text(self._fmt(dur), size=14, w=48, color=theme.SUBTLE_FG),
                tbtn("favorite" if np.get("favorite") else "favorite_border",
                     "np-fav", lambda: self._toggle_np_favorite(),
                     color=theme.FAV_RED if np.get("favorite") else "eeeeee"),
                tbtn("repeat_one" if repeat == "one" else "repeat", "np-repeat",
                     lambda: self._cycle_repeat(),
                     color=theme.ACCENT if repeat != "none" else "888888"),
                Icon("volume_up", 20, color="aaaaaa"),
                Slider("np-vol", value=np.get("volume", 100), min=0, max=100,
                       w=110,
                       on_change=lambda v: self._ctl(lambda c: c.set_volume(v))),
                tbtn("queue_music", "np-queue", self._open_queue),
            ],
            pad=10, gap=10, align="center", h=64, bg=theme.PANEL_BG)

    # ---------------------------------------- route loaders

    def _load_music(self, route, ep):
        def done(res):
            items, total = res
            route["_data"], route["_total"] = items, total
            route["_loading"] = False
        fetch = self._music_fetch(route)     # bind the tab here, not later
        self._route_async(route, lambda: fetch(0), done, ep)

    def _load_album(self, route, ep):
        srv = route.get("server") or self.server
        iid = route["item_id"]

        def work():
            return {"item": self.source.get_item(srv, iid),
                    "tracks": self.source.get_album_tracks(srv, iid)}
        self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)

    def _load_artist(self, route, ep):
        srv = route.get("server") or self.server
        iid = route["item_id"]

        def work():
            songs, similar = [], []
            try:
                songs = self.source.get_artist_songs(srv, iid)
            except Exception:
                pass
            try:
                similar = self.source.get_similar(srv, iid) or []
            except Exception:
                pass   # offline / older server: just no row
            item = {}
            try:
                item = self.source.get_item(srv, iid) or {}
            except Exception:
                pass   # older server / offline: header degrades to the title
            return {"item": item,
                    "albums": self.source.get_artist_albums(srv, iid),
                    "songs": songs, "similar": similar}
        self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)

    def _load_music_genre(self, route, ep):
        srv = route.get("server") or self.server

        def work():
            songs = []
            try:
                songs = self.source.get_genre_songs(
                    srv, route.get("parent_id"), route["item_id"])
            except Exception:
                pass
            albums, total = self.source.get_genre_albums(
                srv, route.get("parent_id"), route["item_id"])
            return {"albums": albums, "songs": songs, "total": total}
        self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)

    def _load_playlist(self, route, ep):
        srv = route.get("server") or self.server
        iid = route["item_id"]

        def work():
            return self.source.get_playlist_items(srv, iid)
        self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)
