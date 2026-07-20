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
from . import theme
from .repository import PLAYABLE_TYPES, PLAYLIST_SUPPORTED_TYPES
from .strips import Tile
from .thumbnails import make_key

log = logging.getLogger("mpvtk_browser.tiles")


class TilesMixin:

    # -------------------------------------------------------- tile helpers

    def _subtitle(self, item):
        if item.get("_subtitle") is not None:
            return item["_subtitle"]      # pseudo-items (chapters)
        if item.get("Type") == "Episode":
            # Lead with the series. A bare "S1E1" on a Continue Watching or
            # Next Up tile does not say which show it belongs to, which is
            # the one thing you need there.
            series = item.get("SeriesName") or ""
            s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
            if s is not None and e is not None:
                se = "S%dE%d" % (s, e)
                return "%s · %s" % (series, se) if series else se
            return series
        return str(item.get("ProductionYear") or "")

    # A thumbnail fetch that fails transiently is retried on a later
    # repaint, but not immediately: a server that is down or slow would
    # otherwise get a fresh burst on every scroll frame. Attempts are
    # capped so a permanently broken URL settles instead of retrying for
    # the life of the session.
    IMG_RETRY_BACKOFF = 5.0    # seconds, doubled per attempt

    IMG_MAX_ATTEMPTS = 4

    def _request_image(self, key, url, box):
        """Return a cached decoded PIL image for ``key`` (poster/backdrop/…),
        or None while it loads — requesting it once from the thumbnail pool.
        The next repaint (woken by the pool's notify) picks it up."""
        img = self._posters.get(key)
        if img is not None or self.thumbs is None or not url:
            return img
        if key in self._requested:
            return None
        retry_at = self._img_retry.get(key)
        if retry_at is not None and time.time() < retry_at[1]:
            return None            # cooling off after a failed attempt
        self._requested.add(key)
        self.thumbs.request(key, url, box,
                            lambda im, k=key: self._image_done(k, im))
        return None

    def _image_done(self, key, image):
        """Thumbnail delivery, on the loop thread.

        ``image`` is None when the fetch failed. Releasing the dedup marker
        is the whole point: it used to be set before dispatch and never
        cleared, so one timed-out poster stayed blank for the rest of the
        process — no navigation, scroll or re-open would ask again. A
        permanent miss (the server says there's no such image) keeps the
        marker, so it isn't asked for again either."""
        if image is not None:
            self._posters[key] = image
            self._img_retry.pop(key, None)
            return
        if self.thumbs is not None and self.thumbs.is_gone(key):
            return                 # no such image; stop asking
        attempts = self._img_retry.get(key, (0, 0.0))[0] + 1
        if attempts > self.IMG_MAX_ATTEMPTS:
            return                 # give up, keeping the marker set
        self._img_retry[key] = (
            attempts,
            time.time() + self.IMG_RETRY_BACKOFF * (2 ** (attempts - 1)))
        self._requested.discard(key)

    def _poster_for(self, item, geom, image_type="Primary"):
        """Return (PIL image or None, cache tag). Requests the poster once
        if absent; the strip recomposites when it arrives (tag changes)."""
        w, h = geom.tile_w, geom.tile_h
        if "_image_url" in item:
            # A pseudo-item (a chapter) carrying its own spec+url: chapter
            # art is indexed, so it isn't addressable through image_spec.
            spec, url = item.get("_image_spec"), item.get("_image_url")
            if not spec or not url:
                return None, ""
            key = make_key(spec[0], spec[1], spec[2], w, h)
            return self._request_image(key, url, (w, h)), key
        spec = self.source.image_spec(item, image_type, geom.tile_w)
        if not spec or self.server is None:
            return None, ""
        item_id, itype, itag = spec
        w, h = geom.tile_w, geom.tile_h
        key = make_key(item_id, itype, itag, w, h)
        url = self.source.image_url(self.server, item_id, itype, itag,
                                    w, h, fill=True)
        return self._request_image(key, url, (w, h)), key

    def _art_cell(self, item, size=28):
        """Small square album-art bitmap for a table cell (track lists);
        a placeholder box while it loads or when the item has none.
        Each cell is its own overlay, so only use in virtualized or
        short tables (the 63-overlay budget is shared)."""
        spec = self.source.image_spec(item, "Primary", size)
        if spec and self.server is not None:
            item_id, itype, itag = spec
            key = make_key(item_id, itype, itag, size, size)
            url = self.source.image_url(self.server, item_id, itype,
                                        itag, size, size, fill=True)
            img = self._request_image(key, url, (size, size))
            if img is not None:
                b = self.strips.bitmap(key, img)
                return Image(b["src"], b["iw"], b["ih"])
        return self._art_placeholder(size)

    @staticmethod
    def _art_placeholder(size=28):
        """Same-sized stand-in for an art cell — while it loads, when the
        item has none, and for rows outside the virtual window (which must
        not composite: see _track_list)."""
        return Box(w=size, h=size, bg=theme.PLACEHOLDER_BG, radius=4)

    def _is_watched(self, item):
        ud = item.get("UserData") or {}
        if ud.get("Played"):
            return True
        if item.get("Type") in ("Series", "Season"):
            # `or 0` would read a MISSING count as zero-unplayed, i.e.
            # fully watched — so a Series DTO without UserData (search
            # results, the synthesized season fallback) showed a watched
            # check, and the toggle computed `not watched` and marked an
            # unwatched show unwatched: a no-op that reads as a dead button.
            return ud.get("UnplayedItemCount") == 0
        return False

    def _is_downloaded(self, item):
        iid, t = item.get("Id"), item.get("Type")
        if iid in self._downloaded:
            return True
        if t == "Series":
            return iid in self._downloaded_series
        # Neither a season nor a playlist is ever itself a downloads row: a
        # season is expanded into its episodes, and a playlist lives in its
        # own table. Without these two a downloaded season showed "Download"
        # forever, never got the tile badge, and the Season branch of
        # _remove_download was unreachable — the same shape as the playlist
        # bug this line was added for.
        if t == "Season":
            return iid in self._downloaded_seasons
        return t == "Playlist" and iid in self._downloaded_playlists

    @staticmethod
    def _glyph(item):
        if item.get("Type") in ("Audio", "MusicAlbum", "MusicArtist"):
            return "♪"  # ♪
        name = (item.get("Name") or "").strip()
        return name[0].upper() if name else "?"

    # A banner is a wide crop, not a 16:9 frame — two-thirds the height of
    # the equivalent 16:9 box, which is roughly 2.4:1.
    BANNER_RATIO = 9 / 16 * 2 / 3

    def _banner_box(self, width):
        bw = min(width - 2 * self.CONTENT_PAD, 1100)
        return bw, int(bw * self.BANNER_RATIO)

    def _backdrop_node(self, item, box, node_id, title=None, meta=None,
                       context=None):
        """A backdrop banner for detail/series headers.

        With ``title`` the heading is *baked into the bitmap* over a bottom
        gradient, like the Tk browser did — text drawn as ASS would sit
        under the image (bitmaps composite above all script ASS), and the
        occlude punch would show the window background rather than the
        artwork. Returns a placeholder Box while the art loads or if the
        item has none, in which case the caller still draws its own heading."""
        spec = None
        if self.server is not None:
            spec = self.source.backdrop_spec(item)
        if spec:
            owner_id, tag = spec
            key = make_key(owner_id, "Backdrop", tag, box[0], box[1])
            if title:
                key += "|" + make_key(title, meta or "", context or "",
                                      box[0], box[1])
            url = self.source.backdrop_url(self.server, item, width=box[0],
                                           height=box[1], fill=True)
            # Request at the *source* aspect and crop to the banner below, so
            # a shallow banner doesn't ask the server for a squashed image.
            img = self._request_image(key, url, (box[0], box[0]))
            if img is not None:
                b = self.strips.bitmap(key, self._compose_banner(
                    img, box, title, meta, context))
                return Image(b["src"], b["iw"], b["ih"], id=node_id)
        return Box(w=box[0], h=box[1], bg=theme.PLACEHOLDER_BG, radius=6,
                   id=node_id)

    @staticmethod
    def _heading_for(item):
        """``(title, context)`` for a detail heading.

        An episode's series and SxEy go on their own line above the episode
        title rather than being joined into one string — joined, a name of
        any length ran off the end of the banner and was cut mid-word
        ("Clannad · S1E1 · On the Hillside Pa")."""
        title = item.get("Name", "")
        if item.get("Type") != "Episode":
            return title, ""
        s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
        se = "S%sE%s" % (s, e) if s is not None and e is not None else ""
        context = "   ·   ".join(
            p for p in (item.get("SeriesName"), se) if p)
        return title, context

    @staticmethod
    def _wrap_pil(draw, text, font, max_w, max_lines=2):
        """Word-wrap ``text`` to ``max_lines``, ellipsizing the last line.
        Falls back to breaking mid-word for a single word too long to fit."""
        words, lines, cur = text.split(), [], ""
        for word in words:
            trial = (cur + " " + word).strip()
            if not cur or draw.textlength(trial, font=font) <= max_w:
                cur = trial
                continue
            lines.append(cur)
            cur = word
            if len(lines) == max_lines:
                break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        if not lines:
            return [text]
        # The last line absorbs whatever didn't fit, ellipsized.
        consumed = len(" ".join(lines).split())
        if consumed < len(words) or draw.textlength(
                lines[-1], font=font) > max_w:
            last = lines[-1]
            if consumed < len(words):
                last = " ".join([last] + words[consumed:])
            while last and draw.textlength(last + "…", font=font) > max_w:
                last = last[:-1]
            lines[-1] = last.rstrip() + "…"
        return lines

    @classmethod
    def _compose_banner(cls, image, box, title=None, meta=None, context=None):
        """Crop ``image`` to the banner box and bake the heading over a
        bottom-up dark gradient.

        Stacked bottom-up: meta, title (wrapped to two lines), then the
        context line above it. Text is sized off the banner height and
        stays small enough that a long episode title reads in full."""
        from PIL import ImageDraw

        from ..imageutil import (apply_dark_gradient, pil_font,
                                 scale_to_cover)

        w, h = box
        canvas = scale_to_cover(image.convert("RGBA"), w, h)
        if not title:
            return canvas
        canvas = apply_dark_gradient(canvas, height_fraction=0.7,
                                     max_alpha=215)
        draw = ImageDraw.Draw(canvas)
        margin = max(18, w // 40)
        avail = w - 2 * margin
        # Smaller than it was: the heading has up to three stacked lines to
        # fit inside the gradient now, not one.
        size = max(20, min(34, h // 6))
        y = h - margin
        if meta:
            f = pil_font(int(size * 0.6), text=meta)
            asc, desc = f.getmetrics()
            draw.text((margin, y - asc - desc), meta, font=f,
                      fill=(200, 200, 200, 255))
            y -= asc + desc + 6
        f = pil_font(size, bold=True, text=title)
        asc, desc = f.getmetrics()
        for line in reversed(cls._wrap_pil(draw, title, f, avail)):
            draw.text((margin, y - asc - desc), line, font=f,
                      fill=(255, 255, 255, 255))
            y -= asc + desc + 2
        if context:
            f = pil_font(int(size * 0.62), text=context)
            asc, desc = f.getmetrics()
            line = cls._wrap_pil(draw, context, f, avail, max_lines=1)[0]
            draw.text((margin, y - asc - desc + 2), line, font=f,
                      fill=(215, 215, 215, 255))
        return canvas

    def _tile(self, item, geom, image_type="Primary"):
        ud = item.get("UserData") or {}
        pos = ud.get("PlaybackPositionTicks") or 0
        rt = item.get("RunTimeTicks") or 0
        poster, tag = self._poster_for(item, geom, image_type)
        return Tile(
            key=item.get("Id", ""),
            title=item.get("Name", ""),
            subtitle=self._subtitle(item),
            poster=poster,
            poster_tag=tag,
            glyph=self._glyph(item),
            watched=self._is_watched(item),
            badge=int(ud.get("UnplayedItemCount") or 0),
            progress=(pos / rt) if (pos and rt) else 0.0,
            downloaded=self._is_downloaded(item),
        )

    def _image_map(self, items, prefix, geom=None, image_type="Primary",
                   on_click=None):
        geom = geom or self.geom
        tiles = [self._tile(it, geom, image_type) for it in items]
        s = self.strips.strip(tiles, geom)
        regions = []
        act = on_click or self._open_item
        for r, it in zip(s["regions"], items):
            regions.append(dict(
                r,
                id="%s-%s" % (prefix, r["key"]),
                on_click=(lambda i=it: act(i)),
                on_context=(lambda x, y, i=it: self._open_tile_menu(i, x, y)),
            ))
        return ImageMap(s["src"], s["iw"], s["ih"], regions=regions)

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

    MENU_FAVORITE = MENU_PLAYABLE | {"MusicAlbum", "MusicArtist"}

    MENU_ADD_TO = PLAYABLE_TYPES | {"Audio", "MusicAlbum", "MusicArtist",
                                    "MusicGenre", "Series", "Season"}

    MENU_DOWNLOAD = PLAYABLE_TYPES | {"Audio", "Series", "Season", "Playlist"}

    def _tile_menu_entries(self, item):
        """``[(label, icon, action-key)]`` for this item's type."""
        t = item.get("Type")
        ud = item.get("UserData") or {}
        watched = self._is_watched(item)
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
        """Whether the apiclient can edit playlists/collections.

        Fails OPEN: only a probe that positively answers False hides the
        affordances. An inconclusive probe (a controller without the
        method) hiding every edit control is a worse failure than showing
        one that errors — and the API call is the real check anyway."""
        if self._edit_apis_ok is None:
            try:
                answer = self.controller.edit_apis()
            except Exception:
                answer = None
            self._edit_apis_ok = answer is not False
        return self._edit_apis_ok

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
        self.run_async(work, done, ep)

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

    # Row height of every track table (album, playlist, queue, songs).
    TRACK_ROW_H = 34

    # Square page-arrow buttons floating over the carousel's edges.
    ARROW_W = 38

    # Slack inside a scroll viewport so a tile's hover ring — which the
    # renderer draws 2px OUTSIDE the hit rect, and clips to the viewport —
    # isn't shaved off against the container edge. Without it the top of the
    # ring vanished under the row heading above.
    RING_PAD = 5

    def _tile_row(self, title, items, row_id, geom=None, image_type="Primary",
                  bleed=False, on_click=None):
        """A titled horizontal carousel.

        ``bleed`` runs the strip edge-to-edge so the page arrows sit flush
        against the window's left and right sides; the title is indented to
        line up with the content instead."""
        geom = geom or self.geom
        heading = Text(title, size=24, bold=True)
        if bleed:
            # The strip runs edge to edge; indent the heading to line up with
            # the first tile instead.
            heading = Row([Spacer(w=self.CONTENT_PAD), heading])
        return Column(
            [
                heading,
                self._hscroll_row(
                    self._image_map(items, row_id, geom, image_type,
                                    on_click=on_click),
                    row_id, geom.strip_h + 2 * self.RING_PAD,
                    len(items), geom, bleed),
            ],
            gap=10,
        )

    def _hscroll_row(self, content, row_id, h, count, geom, bleed=False):
        """An HScroll with ◀ ▶ page buttons floating over its edges.

        The arrows genuinely overlay the poster strip: a Stack layers them on
        top, and ``occlude=True`` punches their rect out of the strip bitmap
        below so the ASS button draws in the hole (bitmaps otherwise composite
        above all script ASS — GUIDE §6). They hold-repeat while pressed, and
        are omitted when the row doesn't overflow.

        The strip is inset by RING_PAD so a tile's hover ring has room inside
        the viewport; the renderer clips it to the container, and without the
        inset its top edge was shaved off under the heading above."""
        scroll = HScroll(Box([content], pad=self.RING_PAD),
                         id=row_id, h=h, flex=1)
        avail = (self._size[0] if self._size else 1280)
        if not bleed:
            avail -= 2 * self.CONTENT_PAD
        content_w = count * geom.tile_w + max(0, count - 1) * geom.gap
        if content_w <= avail or self._nav_mode:
            # keyboard/remote navigation auto-scrolls the row as focus
            # moves — pointer paging arrows would only cover artwork
            return Row([scroll], h=h)

        def arrow(icon, node_id, direction, anchor):
            # Square, and small enough to cover as little artwork as
            # possible — the occlusion punch reads as a notch, so a tall
            # slab looked wrong. Flex spacers centre the glyph (Box only
            # centres on its cross axis).
            return Box([Spacer(flex=1), Icon(icon, 22), Spacer(flex=1)],
                       id=node_id, w=self.ARROW_W, h=self.ARROW_W,
                       align="center", direction="row",
                       bg=theme.BUTTON_BG, alpha=230,
                       hover={"fill": theme.BUTTON_ACTIVE}, radius=6,
                       anchor=anchor, dx=(self.RING_PAD if anchor == "w"
                                          else -self.RING_PAD),
                       # "w"/"e" centre on the whole strip, which includes the
                       # caption block under the tile; shift up by half of it
                       # so the arrow sits on the artwork.
                       dy=-(geom.strip_h - geom.tile_h) / 2,
                       occlude=True, repeat=True,
                       on_click=lambda: self._page_row(row_id, direction))

        return Stack([
            scroll,
            arrow("chevron_left", row_id + "-pl", -1, "w"),
            arrow("chevron_right", row_id + "-pr", 1, "e"),
        ], h=h)

    def _page_row(self, row_id, direction):
        # Ask the renderer to page the horizontal scroll container.
        if self.app is not None and hasattr(self.app, "scroll"):
            self.app.scroll(row_id, direction)

    # ---------------------------------------------------- music / playlists

    def _cols(self, w, geom):
        # _body_w, not w - 32: grids sit in the same padded scroll column,
        # so ignoring the scrollbar fits one tile too many at some widths
        # and the last one is clipped.
        return max(1, int(
            (self._body_w(w) + geom.gap) // (geom.tile_w + geom.gap)))

    GRID_GAP = 12

    def _grid_of(self, items, prefix, size, heading=None, geom=None,
                 image_type="Primary", scroll_id=None, head_h=0,
                 on_click=None):
        """Tile rows for a vertical grid.

        With ``scroll_id`` the rows are **virtualized**: only those within a
        screen of the viewport are composited, the rest become fixed-height
        Spacers. Without it a long library blows past both the strip cache and
        mpv's 63-overlay budget, which showed up as tiles that came back blank
        after scrolling away and back."""
        geom = geom or self.geom
        cols = self._cols(size[0], geom)
        rows = [Text(heading, size=26, bold=True)] if heading else []
        nrows = (len(items) + cols - 1) // cols
        first, last = 0, nrows - 1
        if scroll_id is not None:
            rh = geom.strip_h + self.GRID_GAP
            vh = max(240.0, float(size[1]))
            top = max(0.0, self._offset(scroll_id) - head_h)
            first = int(max(0.0, top - vh) // rh)
            last = int((top + 2 * vh) // rh)
        for r in range(nrows):
            if first <= r <= last:
                start = r * cols
                rows.append(self._image_map(items[start:start + cols],
                                            "%s-%d" % (prefix, start),
                                            geom, image_type,
                                            on_click=on_click))
            else:
                rows.append(Spacer(h=geom.strip_h))
        if not items:
            rows.append(Text(_("Nothing here yet."), size=18,
                             color=theme.SUBTLE_FG))
        return rows

    def _track_list(self, tracks, prefix, on_play, playing_id=None,
                    selected=None, on_select=None, album=True,
                    art=False, scroll_id=None, head_h=0, menu=False):
        """Tabular track list (album, playlist, queue, search songs).

        Uses the toolkit's Table so header and cells come from one column
        spec — hand-laid Rows drifted out of alignment as soon as a cell's
        text width changed.

        With ``on_select`` the row click selects (mods-aware) and the first
        column becomes a play button, so a selectable list still has a
        one-click way to jump to a track. Without it, clicking the row plays.

        ``art=True`` adds a leading album-art thumbnail column — useful in
        mixed-album lists (playlists); redundant on an album page."""
        selected = selected or set()
        columns = []
        if art:
            columns.append({"label": "", "w": 32})
        columns += [{"label": "#", "w": 46, "align": "right"},
                    {"label": _("Title"), "flex": 3},
                    {"label": _("Artist"), "flex": 2}]
        if album:
            columns.append({"label": _("Album"), "flex": 2})
        columns.append({"label": _("Time"), "w": 70, "align": "right"})

        def first_cell(i, tr):
            if on_select is None:
                return str(tr.get("IndexNumber") or (i + 1))
            return Box([Icon("play_arrow", 16,
                             color=theme.ACCENT if tr.get("Id") == playing_id
                             else theme.SUBTLE_FG)],
                       id="%s-play-%d" % (prefix, i), w=40, h=26,
                       # justify centres along the row's main axis; align
                       # alone only centred it vertically, leaving the glyph
                       # packed against the left edge of the button.
                       direction="row", align="center", justify="center",
                       radius=4,
                       hover={"fill": theme.BUTTON_ACTIVE},
                       on_click=lambda i=i: on_play(i))

        # Virtualize against the live scroll offset. Not just a repaint
        # cost: with art=True each visible row is one mpv overlay, and a
        # few hundred tracks would blow the 63-overlay budget outright.
        virtual = None
        if scroll_id is not None and self._size is not None:
            virtual = {"offset": max(0.0, self._offset(scroll_id) - head_h),
                       "height": float(self._size[1])}
        # The window has to be known HERE, not just inside Table: art cells
        # composite a bitmap into the 48-entry strip LRU as they are built,
        # so building them for every row of a long playlist evicted (and
        # freed the backing buffer of) the very rows on screen — they drew
        # blank, deterministically, on every repaint.
        art_first, art_last = virtual_window(virtual, self.TRACK_ROW_H,
                                             len(tracks))

        rows = []
        for i, tr in enumerate(tracks):
            playing = playing_id is not None and tr.get("Id") == playing_id
            cells = [first_cell(i, tr), tr.get("Name", ""), self._artists(tr)]
            if art:
                cells.insert(0, self._art_cell(tr)
                             if art_first <= i < art_last
                             else self._art_placeholder())
            if album:
                cells.append(tr.get("Album", "") or "")
            cells.append(self._duration(tr))
            row = {
                "id": "%s-%d" % (prefix, i),
                "selected": i in selected or playing,
                "cells": cells,
                "on_click": ((lambda mods, i=i: on_select(i, mods))
                             if on_select is not None
                             else (lambda i=i: on_play(i))),
            }
            if menu:
                # Right-click a track for the same menu a tile gets. Tiles
                # have had this all along; a Table row never asked for it, so
                # every music playlist lost Play/Queue/Favorite/Download —
                # and per-track "Remove from Playlist" entirely, leaving only
                # the bulk editor.
                row["on_context"] = (
                    lambda x, y, tr=tr: self._open_tile_menu(tr, x, y))
            rows.append(row)
        return Table(columns, rows, size=17, row_h=self.TRACK_ROW_H,
                     hover_bg=theme.BUTTON_BG, virtual=virtual)
