"""The main content routes.

Home, grid (a library), detail, series, season and search, plus the
detail-page pieces: track pickers, action buttons and the media-info line.

State on ``self``: none of its own — every view keeps its data in the route
dict and every mutation ends with ``invalidate()``. Handlers here run on
the loop thread and must capture route state *before* dispatching async
work; reading ``self.route`` inside the callback races navigation.
"""

import logging

from ..i18n import _
from ..mpvtk.widgets import (
    Box,
    Busy,
    Button,
    Checkbox,
    Column,
    Dropdown,
    Icon,
    Row,
    Spacer,
    Text,
    VScroll,
)
from . import theme

log = logging.getLogger("mpvtk_browser.views")

# Grid sort modes (label, SortBy, SortOrder) — ported from the Tk browser.
SORTS = [
    (_("Name"), "SortName", "Ascending"),
    (_("Date Added"), "DateCreated", "Descending"),
    (_("Release Date"), "PremiereDate", "Descending"),
    (_("Community Rating"), "CommunityRating", "Descending"),
    (_("Date Played"), "DatePlayed", "Descending"),
    (_("Play Count"), "PlayCount", "Descending"),
    (_("Runtime"), "Runtime", "Ascending"),
    (_("Critic Rating"), "CriticRating", "Descending"),
    (_("Parental Rating"), "OfficialRating", "Ascending"),
    (_("Random"), "Random", "Ascending"),
]
_LETTERS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class ViewsMixin:

    # kind -> (loader, renderer) method names. Merged into
    # one dispatch table by core's _routes().
    ROUTES = {
        "detail": ("_load_detail", "_render_detail"),
        "grid": ("_load_grid", "_render_grid"),
        "home": ("_load_home", "_render_home"),
        "person": ("_load_person", "_render_grid"),
        "search": ("_load_search", "_render_search"),
        "season": ("_load_season", "_render_season"),
        "series": ("_load_series", "_render_series"),
    }

    def _render_home(self, route, size):
        if self.server is None:
            return Box(
                [Spacer(),
                 Row([Spacer(), Busy(), Spacer()]),
                 Row([Spacer(),
                      Text(_("Connecting to your server…"), size=20,
                           color=theme.SUBTLE_FG),
                      Spacer()]),
                 Spacer()],
                flex=1, direction="column", align="stretch", gap=16)
        data = route.get("_data")
        if data is None:
            return self._busy()
        rows = []
        if data["libraries"]:
            # Libraries read as landscape cards, like the web client.
            rows.append(self._tile_row(
                _("Libraries"), data["libraries"], "row-libs",
                geom=self.geom_wide, bleed=True))
        for i, hr in enumerate(data["rows"]):
            if hr.get("items"):
                geom, itype = self._row_shape(hr)
                rows.append(self._tile_row(
                    hr["title"], hr["items"], "row-%d" % i,
                    geom=geom, image_type=itype, bleed=True))
        if not rows:
            rows.append(Row([Spacer(w=self.CONTENT_PAD),
                             Text(_("Nothing to show yet."), size=20,
                                  color=theme.SUBTLE_FG)]))
        # pad=0: home carousels bleed to the window edges so their page
        # arrows sit flush against them (see _hscroll_row).
        return VScroll(Column(rows, gap=20), id="home", flex=1)

    # Item types whose artwork is square, not a 2:3 poster: music, and
    # playlists (whose own Primary image is a square). Rendering them in a
    # poster frame pillarboxes the art.
    SQUARE_TYPES = {"Playlist", "MusicAlbum", "MusicArtist", "Audio",
                    "MusicGenre"}

    def _square_geom(self, items):
        """``geom_square`` when every item's art is square, else None.

        A strip is composited at one tile size, so this is a per-grid
        decision, not per-tile — hence "every item"."""
        types = {i.get("Type") for i in items or ()}
        if types and types <= self.SQUARE_TYPES:
            return self.geom_square
        return None

    def _row_shape(self, hr):
        """(geom, image_type) for a home row, classified like the Tk browser:
        movies/tv/boxsets -> poster; music/playlists -> square; home-video/misc
        or episode-bearing rows -> landscape Thumb."""
        ctype = hr.get("collection_type")
        items = hr.get("items", [])
        has_episode = any(it.get("Type") == "Episode" for it in items)
        if ctype in ("movies", "tvshows", "boxsets"):
            return self.geom, "Primary"
        if ctype in ("music", "playlists"):
            return self.geom_square, "Primary"
        # An untyped row of playlists/music (offline, the mixed rows) still
        # gets square art.
        if self._square_geom(items):
            return self.geom_square, "Primary"
        if ctype:
            return self.geom_wide, ("Thumb" if has_episode else "Primary")
        if has_episode:
            return self.geom_wide, "Thumb"
        return self.geom, "Primary"

    def _render_grid(self, route, size):
        items = route.get("_items")
        if items is None:
            return self._busy()
        header = [Text(route.get("title", ""), size=26, bold=True)]
        if route["kind"] == "grid":
            if route.get("_collection_capable"):
                header.append(Row([
                    Checkbox(_("Collections"),
                             bool(route.get("_collections")),
                             id="grid-collections",
                             on_toggle=lambda: self._toggle_collections(
                                 route))], gap=10, align="center"))
            header.append(self._grid_filter_bar(route))
            total = route.get("_total") or 0
            header.append(Text(_("%(shown)d of %(total)d") % {
                "shown": len(items), "total": total},
                size=14, color=theme.SUBTLE_FG))
        # Header height (title + optional filter bar + count) so the
        # virtualizer can map a scroll offset onto a tile row.
        head_h = 40 + (110 if route["kind"] == "grid" else 0)
        rows = header + self._grid_of(
            items, "grid", size, geom=self._square_geom(items) or self.geom,
            scroll_id="grid", head_h=head_h)
        return VScroll(
            Column(rows, pad=self.CONTENT_PAD, gap=self.GRID_GAP,
                   align="stretch"), id="grid",
            flex=1,
            on_scroll=lambda off, mx: self._on_scroll(
                "grid", off, mx,
                lambda o, m: self._on_grid_scroll(route, o, m)),
        )

    def _grid_filter_bar(self, route):
        vals = route.get("_filtervals") or {}
        filters = route.get("_filters") or {}
        genres = vals.get("genres") or []
        gi = 0
        if filters.get("genre") in genres:
            gi = genres.index(filters["genre"]) + 1
        # Years come back as ints; keep them that way in the filter (the
        # offline source compares against ProductionYear directly) and only
        # stringify for display.
        years = list(vals.get("years") or [])
        yi = 0
        if filters.get("year") in years:
            yi = years.index(filters["year"]) + 1
        bar = Row([
            Dropdown("grid-sort", [s[0] for s in SORTS],
                     selected=route.get("_sort", 0), w=180,
                     on_select=lambda i, v: self._set_grid("_sort", route, i)),
            Dropdown("grid-genre", [_("All Genres")] + genres, selected=gi,
                     w=180,
                     on_select=lambda i, v: self._set_grid_filter(
                         route, "genre", None if i == 0 else genres[i - 1])),
            Dropdown("grid-year",
                     [_("All Years")] + [str(y) for y in years],
                     selected=yi, w=140,
                     on_select=lambda i, v: self._set_grid_filter(
                         route, "year", None if i == 0 else years[i - 1])),
            Checkbox(_("Unplayed"), bool(filters.get("unplayed")),
                     id="grid-unplayed",
                     on_toggle=lambda: self._toggle_grid_filter(
                         route, "unplayed")),
            Checkbox(_("Favorites"), bool(filters.get("favorite")),
                     id="grid-fav",
                     on_toggle=lambda: self._toggle_grid_filter(
                         route, "favorite")),
            Spacer(),
            Button(_("Shuffle"), id="grid-shuffle",
                   on_click=lambda: self._grid_shuffle(route)),
        ], gap=10, align="center")
        cur_letter = filters.get("letter")
        letters = Row([
            # flex + align="center" centres the glyph horizontally; a bare
            # Text is packed at the box's left edge (Box only centres on its
            # cross axis), which left every letter hugging its left border.
            Box([Text(ch, size=15, align="center", flex=1,
                      color=theme.ACCENT_FG if cur_letter == ch
                      else theme.SUBTLE_FG)],
                id="grid-l-" + ch, w=26, h=26, align="center", direction="row",
                radius=4, bg=theme.ACCENT if cur_letter == ch else None,
                hover=None if cur_letter == ch else {"fill": theme.BUTTON_BG},
                on_click=lambda c=ch: self._set_grid_filter(
                    route, "letter", None if cur_letter == c else c))
            for ch in _LETTERS], gap=2, align="center")
        return Column([bar, letters], gap=8)

    def _reload_grid(self, route):
        for k in ("_items", "_total"):
            route.pop(k, None)
        route["_loading"] = False
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def _set_grid(self, key, route, value):
        route[key] = value
        self._reload_grid(route)

    def _set_grid_filter(self, route, key, value):
        route.setdefault("_filters", {})[key] = value
        self._reload_grid(route)

    def _toggle_grid_filter(self, route, key):
        f = route.setdefault("_filters", {})
        f[key] = not f.get(key)
        self._reload_grid(route)

    def _grid_shuffle(self, route):
        srv = route.get("server") or self.server
        ep = self._epoch

        def work():
            return self.source.get_shuffle_ids(srv, route["parent_id"])

        def done(ids):
            if ids:
                self._play_list(ids, srv, 0)
        self.run_async(work, done, ep)

    def _on_grid_scroll(self, route, offset, maximum):
        # Read on the loop thread, before dispatch: the sort/filters must be
        # the ones the page was asked for, not whatever they are when it lands.
        _n, sort_by, sort_order = SORTS[route.get("_sort", 0)]
        filters = route.get("_filters") or {}
        person = route.get("person_id")

        def fetch(start):
            srv = route.get("server") or self.server
            if person:
                return self.source.get_person_items(srv, person,
                                                    start_index=start)
            if route.get("_collections"):
                return self.source.get_movie_collections(
                    srv, start_index=start, sort_by=sort_by,
                    sort_order=sort_order, filters=filters)
            return self.source.get_library_items(
                srv, route["parent_id"], start_index=start, sort_by=sort_by,
                sort_order=sort_order, filters=filters)

        def put(r, items, total):
            r["_items"], r["_total"] = items, total

        self._page_more(
            route, offset, maximum,
            lambda r: (r.get("_items") or [], r.get("_total") or 0),
            put, fetch)

    # --------------------------------------------------- detail / series / etc

    def _meta_line(self, item):
        parts = []
        if item.get("ProductionYear"):
            parts.append(str(item["ProductionYear"]))
        rt = item.get("RunTimeTicks")
        if rt:
            parts.append(_("%d min") % (rt // 600000000))
        if item.get("OfficialRating"):
            parts.append(str(item["OfficialRating"]))
        if item.get("CommunityRating"):
            parts.append("★ %.1f" % item["CommunityRating"])
        return "   ·   ".join(parts)

    def _body_w(self, w):
        """Usable text width inside a padded, scrollable content column.

        The window width minus the content padding AND the scrollbar the
        scroll view reserves. Wrapping at ``w - 2*pad`` — the padding alone
        — makes every line 10px wider than the space it actually gets, so
        the tail of each line runs under the scrollbar, and which words land
        there changes with the window size. That is what made resizing look
        like the wrapping was unstable."""
        from ..mpvtk.layout import SCROLLBAR_W

        return max(120, w - 2 * self.CONTENT_PAD - SCROLLBAR_W)

    def _paragraph(self, text, size, max_w, color=None):
        """Wrapped body text (overviews).

        The layout engine wraps *within* a paragraph, so blank-line breaks
        are handled here. The gap is a full line height: at anything less
        the paragraph break reads as tighter than the wrapped lines around
        it, which looks like a mistake rather than a break."""
        from ..mpvtk.layout import LINE_H

        paras = [p.strip() for p in (text or "").replace("\r", "").split("\n")
                 if p.strip()]
        color = color or theme.TEXT_FG
        if len(paras) <= 1:
            return Text(paras[0] if paras else "", size=size, color=color,
                        wrap=True, w=max_w)
        return Column([Text(p, size=size, color=color, wrap=True, w=max_w)
                       for p in paras],
                      gap=round(size * LINE_H), w=max_w)

    def _sel_source(self, sources, route):
        if not sources:
            return None
        return next((s for s in sources
                     if s.get("Id") == route.get("_srcid")), sources[0])

    def _pick_source(self, route, src):
        route["_srcid"] = src.get("Id")
        route["_aid"] = None   # let the new version pick its own defaults
        route["_sid"] = None
        self.invalidate()

    def _default_track_indices(self, route, src, item):
        """``(aid, sid)`` playback will actually choose for ``src``:
        language_config first, then the server's session default — the same
        resolution media.map_streams performs.

        The pickers have to show these rather than a bare "None". A browser
        selection is taken as final downstream (``explicit_tracks``, which
        makes map_streams skip its own defaulting), so a picker that
        misreports the default doesn't just look wrong — it makes playback
        obey the lie, and remember_subtitle_track then pins it for the rest
        of the queue.

        Cached per media source: this is reached from build(), i.e. once a
        repaint, and apply() does real work and logs every call."""
        cache = route.setdefault("_def_tracks", {})
        key = (src or {}).get("Id")
        if key in cache:
            return cache[key]
        aid = sid = None
        if src:
            try:
                from ..conf import settings
                from ..language_config import apply as apply_language_config

                aid, sid = apply_language_config(
                    settings.language_config, src, item)
            except Exception:
                log.debug("language_config lookup failed", exc_info=True)
                aid = sid = None
            if aid is None:
                aid = src.get("DefaultAudioStreamIndex")
            if sid is None:
                sid = src.get("DefaultSubtitleStreamIndex")
        cache[key] = (aid, sid)
        return aid, sid

    def _effective_tracks(self, route, item):
        """``(aid, sid)`` the pickers display and playback is started with:
        the user's pick where they made one, otherwise the resolved default.

        Both are sent, not just the one that was touched — mirroring the Tk
        browser, whose comboboxes are always populated. Sending only the
        touched one marks the play explicit and map_streams then returns
        before defaulting the other, which is how picking an audio track
        silently turned the subtitles off."""
        src = self._sel_source(item.get("MediaSources") or [], route)
        streams = (src or {}).get("MediaStreams") or []
        def_aid, def_sid = self._default_track_indices(route, src, item)
        aid, sid = route.get("_aid"), route.get("_sid")
        # Only default a kind that actually has streams, so an item with no
        # subtitles isn't reported as a deliberate choice.
        if aid is None and any(s.get("Type") == "Audio" for s in streams):
            aid = def_aid
        if sid is None and any(s.get("Type") == "Subtitle" for s in streams):
            sid = def_sid
        return aid, sid

    def _track_pickers(self, route, item):
        sources = item.get("MediaSources") or []
        controls = []
        if len(sources) > 1:
            names = [s.get("Name") or _("Version %d") % (i + 1)
                     for i, s in enumerate(sources)]
            cur = next((i for i, s in enumerate(sources)
                        if s.get("Id") == route.get("_srcid")), 0)
            controls.append(self._picker_row(
                _("Version"), "dt-version", names, cur,
                lambda i, v: self._pick_source(route, sources[i])))
        src = self._sel_source(sources, route)
        streams = (src or {}).get("MediaStreams") or []
        audio = [s for s in streams if s.get("Type") == "Audio"]
        subs = [s for s in streams if s.get("Type") == "Subtitle"]

        def label(s, kind):
            return (s.get("DisplayTitle") or s.get("Language")
                    or "%s %s" % (kind, s.get("Index")))
        # What the pickers show must be what will play — see _effective_tracks.
        eff_aid, eff_sid = self._effective_tracks(route, item)
        if audio:
            names = [label(s, _("Audio")) for s in audio]
            cur = next((i for i, s in enumerate(audio)
                        if s.get("Index") == eff_aid), 0)
            controls.append(self._picker_row(
                _("Audio"), "dt-audio", names, cur,
                lambda i, v: route.__setitem__("_aid", audio[i].get("Index"))))
        if subs:
            names = [_("None")] + [label(s, _("Sub")) for s in subs]
            cur = 0
            if eff_sid not in (None, -1):
                cur = next((i + 1 for i, s in enumerate(subs)
                            if s.get("Index") == eff_sid), 0)
            controls.append(self._picker_row(
                _("Subtitle"), "dt-sub", names, cur,
                lambda i, v: route.__setitem__(
                    "_sid", -1 if i == 0 else subs[i - 1].get("Index"))))
        return controls

    def _picker_row(self, label, node_id, names, selected, on_select):
        return Row([Text(label, w=90, size=16, color=theme.SUBTLE_FG),
                    Dropdown(node_id, names, selected=selected, w=300,
                             on_select=on_select)], gap=8, align="center")

    @staticmethod
    def _fmt_ticks(ticks):
        """h:mm:ss / m:ss — a bare minutes:seconds rendered a 1h20m resume
        offset as "80:00"."""
        secs = int((ticks or 0) // 10000000)
        h, m, sec = secs // 3600, (secs % 3600) // 60, secs % 60
        return ("%d:%02d:%02d" % (h, m, sec) if h
                else "%d:%02d" % (m, sec))

    def _play_buttons(self, route, item, server, trailers=None):
        ud = item.get("UserData") or {}
        pos = ud.get("PlaybackPositionTicks") or 0
        srcid = (route.get("_srcid")
                 or ((item.get("MediaSources") or [{}])[0]).get("Id"))
        aid, sid = self._effective_tracks(route, item)
        buttons = []
        if pos > 0:
            buttons.append(self._action_btn(
                "play_arrow", _("Resume") + "  " + self._fmt_ticks(pos),
                "btn-resume",
                lambda: self._play(item, server, offset_ticks=pos,
                                   srcid=srcid, aid=aid, sid=sid),
                primary=True, size=18))
        buttons.append(self._action_btn(
            "play_arrow", _("Play"), "btn-play",
            lambda: self._play(item, server, srcid=srcid, aid=aid, sid=sid),
            primary=(pos <= 0), size=18))
        tids = [t.get("Id") for t in (trailers or []) if t.get("Id")]
        if tids:
            buttons.append(self._action_btn(
                "movie", _("Trailer"), "btn-trailer",
                lambda: self._play_list(tids, server, 0), size=18))
        return Row(buttons, gap=10)

    def _scenes_row(self, route, item, server):
        """The chapter carousel ("Scenes"), each tile seeking to its start.

        Chapter art is indexed rather than tagged, so the tiles carry a
        ready-made image spec+url (see _poster_for) — image_spec can't
        address it."""
        chapters = item.get("Chapters") or []
        if len(chapters) < 2:
            return None          # a single chapter is just the start
        iid = item.get("Id")
        tiles = []
        for i, ch in enumerate(chapters):
            url = None
            try:
                url = self.source.chapter_image_url(server, iid, i, ch,
                                                    width=self.geom_wide.tile_w)
            except Exception:
                log.debug("chapter art failed", exc_info=True)
            start = ch.get("StartPositionTicks") or 0
            tiles.append({
                "Id": "%s#ch%d" % (iid, i),
                "Name": ch.get("Name") or _("Chapter %d") % (i + 1),
                "Type": "Chapter",
                "_start_ticks": start,
                "_subtitle": self._fmt_ticks(start),
                "_image_spec": ((iid, "Chapter%d" % i,
                                 ch.get("ImageTag") or "none") if url else None),
                "_image_url": url,
            })
        # Starting at a chapter has to carry the same version and tracks
        # the Play button would — Tk's chapter click routes through the
        # detail view's own _play for exactly that reason.
        srcid = (route.get("_srcid")
                 or ((item.get("MediaSources") or [{}])[0]).get("Id"))
        aid, sid = self._effective_tracks(route, item)
        return self._tile_row(
            _("Scenes"), tiles, "detail-scenes", geom=self.geom_wide,
            on_click=lambda t: self._play(
                item, server, offset_ticks=t.get("_start_ticks") or 0,
                srcid=srcid, aid=aid, sid=sid))

    def _action_btn(self, icon, text, node_id, cb, on=False, primary=False,
                    size=16):
        """An icon+label action button.

        ``primary`` is the accent-filled call to action (Play, Next Up);
        ``on`` is a *toggle* that happens to share the accent fill (Watched,
        Favorite). Both use white on blue — black on blue read as disabled.

        Every button in an action row must come from here, icon or not: the
        plain Button widget defaults to a 20px label against this one's 16,
        which made the odd trailing button ~5px taller than its neighbours.
        """
        accent = on or primary
        fg = theme.ACCENT_FG if accent else theme.TEXT_FG
        children = []
        if icon:
            children.append(Icon(icon, size + 2, color=fg))
        children.append(Text(text, size=size, color=fg))
        return Row(children,
                   id=node_id, gap=7, pad=10,
                   bg=theme.ACCENT if accent else theme.BUTTON_BG,
                   hover={"fill": theme.ACCENT_HOVER if accent
                          else theme.BUTTON_ACTIVE},
                   radius=6, align="center", on_click=cb)

    def _common_actions(self, item, server, prefix):
        """Watched / Favorite / Download buttons shared by detail/series/
        season."""
        ud = item.get("UserData") or {}
        return [
            self._action_btn(
                "check", _("Watched"), prefix + "-watched",
                lambda: self._act_watched(item, server),
                on=self._is_watched(item)),
            self._action_btn(
                "favorite", _("Favorite"), prefix + "-fav",
                lambda: self._act_favorite(item, server),
                on=bool(ud.get("IsFavorite"))),
            self._download_btn(item, server, prefix),
        ]

    def _download_btn(self, item, server, prefix):
        """Download, or Remove when it's already downloaded.

        The button used to always say Download, so pressing it on a
        complete item did nothing visible and there was no way to reclaim
        the space outside Settings -> Downloads."""
        if not self._is_downloaded(item):
            return self._action_btn(
                "file_download", _("Download"), prefix + "-download",
                lambda: self._open_download(item))
        return self._action_btn(
            "delete", _("Remove Download"), prefix + "-undownload",
            lambda: self._confirm(
                _("Delete the downloaded copy of %s?")
                % item.get("Name", ""),
                lambda: self._remove_download(item),
                title=_("Delete Download"), yes=_("Delete")))

    def _remove_download(self, item):
        """Delete this item's download, then refresh the badges."""
        iid, t = item.get("Id"), item.get("Type")
        ep = self._epoch

        def work():
            if t == "Series":
                self.controller.delete_download(series_id=iid)
            elif t == "Season":
                self.controller.delete_download(
                    series_id=item.get("SeriesId"), season_id=iid)
            elif t == "Playlist":
                self.controller.delete_download(playlist_id=iid)
            else:
                self.controller.delete_download(item_id=iid)

        def done(_ok):
            self._refresh_downloaded()

        def failed(_exc):
            self.set_status(_("The download could not be removed."))
        self.run_async(work, done, ep, on_error=failed)

    def _detail_actions(self, item, server):
        btns = self._common_actions(item, server, "act")
        if item.get("Type") == "Episode" and item.get("SeriesId"):
            btns.append(self._action_btn(
                "movie", _("Go to Series"), "act-series",
                lambda: self.navigate({
                    "kind": "series", "server": server,
                    "item_id": item["SeriesId"],
                    "title": item.get("SeriesName", "")})))
        return Row(btns, gap=8, align="center")

    def _play_next_up(self, series_id, server):
        ep = self._epoch

        def work():
            item = self.source.get_next_up(server, series_id)
            if item is None:
                # A series nobody has started has no "next up" — the button
                # did nothing at all. Start at the beginning, as Tk does.
                first = self.source.get_series_queue(server, series_id,
                                                     limit=1)
                item = first[0] if first else None
            return item

        def done(item):
            if item:
                # Resume where it was left: Next Up on a part-watched
                # episode restarted it from zero.
                offset = ((item.get("UserData") or {})
                          .get("PlaybackPositionTicks")) or None
                self._play(item, server, offset_ticks=offset)
        self.run_async(work, done, ep)

    def _series_actions(self, item, server, series_id):
        btns = [self._action_btn(
            "play_arrow", _("Next Up"), "sa-nextup",
            lambda: self._play_next_up(series_id, server), primary=True),
            self._action_btn(
                "shuffle", _("Shuffle"), "sa-shuffle",
                lambda: self._shuffle_series(series_id, server))]
        btns += self._common_actions(item, server, "sa")
        return Row(btns, gap=8, align="center")

    def _shuffle_series(self, series_id, server):
        """Shuffle the whole show, like Tk's series-page Shuffle."""
        ep = self._epoch

        def work():
            return [e.get("Id") for e in
                    self.source.get_series_queue(server, series_id,
                                                 limit=200)
                    if e.get("Id")]

        def done(ids):
            if ids:
                self._play_shuffle(ids, server, audio=False)
        self.run_async(work, done, ep)

    def _act_watched(self, item, server):
        ud = item.setdefault("UserData", {})
        was_played, was_count = ud.get("Played"), ud.get("UnplayedItemCount")
        new = not self._is_watched(item)
        ud["Played"] = new
        if item.get("Type") in ("Series", "Season"):
            ud["UnplayedItemCount"] = 0 if new else 1

        def work():
            # Roll the optimistic flip back if nothing recorded it (offline
            # un-watching, or nothing downloaded to queue against). Leaving
            # the tick up meant the UI claimed a change that never happened
            # and quietly reverted on the next reload.
            ok = self.controller.set_watched(server, item.get("Id"), new)
            if ok is False:
                ud["Played"] = was_played
                if was_count is None:
                    ud.pop("UnplayedItemCount", None)
                else:
                    ud["UnplayedItemCount"] = was_count
                self.invalidate()
        self._pool.submit(lambda: self._safe(lambda _c: work()))
        self.invalidate()

    def _act_favorite(self, item, server):
        ud = item.setdefault("UserData", {})
        was = ud.get("IsFavorite")
        new = not bool(was)
        ud["IsFavorite"] = new

        def work():
            # Roll back when nothing recorded it, as _act_watched does —
            # offline the heart used to lie until the next reload.
            if self.controller.set_favorite(server, item.get("Id"),
                                            new) is False:
                ud["IsFavorite"] = was
                self.invalidate()
        self._pool.submit(lambda: self._safe(lambda _c: work()))
        self.invalidate()

    def _media_info_line(self, item, route):
        """Codec/resolution/audio/size line plus "Ends at", like
        jellyfin-web — enough to judge direct-play before hitting Play."""
        import datetime

        src = self._sel_source(item.get("MediaSources") or [], route)
        streams = (src or {}).get("MediaStreams") or []
        parts = []
        video = next((s for s in streams if s.get("Type") == "Video"), None)
        if video:
            if video.get("DisplayTitle"):
                parts.append(video["DisplayTitle"])
            elif video.get("Height"):
                parts.append("%dp" % video["Height"])
            # VideoRangeType first: VideoRange only says HDR, not which.
            vrange = video.get("VideoRangeType") or video.get("VideoRange")
            if vrange and vrange != "SDR":
                parts.append(vrange)
        audio = next((s for s in streams if s.get("Type") == "Audio"), None)
        if audio:
            bits = [(audio.get("Codec") or "").upper(),
                    audio.get("ChannelLayout") or ""]
            joined = " ".join(b for b in bits if b)
            if joined:
                parts.append(joined)
        if src and src.get("Container"):
            parts.append(src["Container"].upper())
        if src and src.get("Size"):
            parts.append(self._human_size(src["Size"]))
        if src and src.get("Bitrate"):
            parts.append(_("%.1f Mbps") % (src["Bitrate"] / 1000000.0))
        runtime = item.get("RunTimeTicks")
        if runtime:
            pos = (item.get("UserData") or {}).get(
                "PlaybackPositionTicks") or 0
            remaining = max(runtime - pos, 0) // 10000000
            ends = (datetime.datetime.now()
                    + datetime.timedelta(seconds=remaining))
            parts.append(_("Ends at %s") % ends.strftime("%H:%M"))
        return "   ·   ".join(p for p in parts if p)

    def _people_row(self, people, server):
        # Every credited person, not just Actor/Director/Writer — Producer,
        # GuestStar and Composer were silently dropped. Copied, not
        # mutated: these DTOs are shared with whatever else holds the item.
        cast = [dict(p, Type="Person", _subtitle=(p.get("Role") or ""))
                for p in people][:24]
        if not cast:
            return None
        return self._tile_row(_("Cast & Crew"), cast, "detail-people",
                              geom=self.geom_square)

    def _error(self, msg):
        return Box([Text(msg, size=20, color=theme.SUBTLE_FG)],
                   pad=24, flex=1, align="center", direction="row")

    def _render_detail(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        item = data.get("item")
        if not item:
            return self._error(_("Item not available."))
        w = size[0]
        server = route.get("server") or self.server
        bw, bh = self._banner_box(w)
        title, context = self._heading_for(item)
        meta = self._meta_line(item)
        banner = self._backdrop_node(item, (bw, bh), "detail-bd",
                                     title=title, meta=meta, context=context)
        blocks = [banner]
        if isinstance(banner, Box):
            # No artwork (or still loading): draw the heading normally, with
            # the same title/context split the baked one uses.
            if context:
                blocks.append(Text(context, size=17, color=theme.SUBTLE_FG))
            blocks.append(Text(title, size=26, bold=True, wrap=True,
                               w=self._body_w(w)))
            if meta:
                blocks.append(Text(meta, size=18, color=theme.SUBTLE_FG))
        info = self._media_info_line(item, route)
        if info:
            blocks.append(Text(info, size=15, color=theme.SUBTLE_FG))
        blocks.append(self._play_buttons(route, item, server,
                                         trailers=data.get("trailers")))
        blocks.append(self._detail_actions(item, server))
        blocks.extend(self._track_pickers(route, item))
        if item.get("Overview"):
            blocks.append(self._paragraph(item["Overview"], 18, self._body_w(w)))
        scenes = self._scenes_row(route, item, server)
        if scenes is not None:
            blocks.append(scenes)
        people_row = self._people_row(item.get("People") or [], server)
        if people_row is not None:
            blocks.append(people_row)
        if data.get("similar"):
            blocks.append(self._tile_row(
                _("More Like This"), data["similar"], "detail-similar"))
        return VScroll(Column(blocks, pad=16, gap=16), id="detail", flex=1)

    def _render_series(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        item = data.get("item") or {}
        w = size[0]
        bw, bh = self._banner_box(w)
        server = route.get("server") or self.server
        meta = self._meta_line(item)
        banner = self._backdrop_node(item, (bw, bh), "series-bd",
                                     title=item.get("Name", ""), meta=meta)
        blocks = [banner]
        if isinstance(banner, Box):
            blocks.append(Text(item.get("Name", ""), size=30, bold=True))
            if meta:
                blocks.append(Text(meta, size=18, color=theme.SUBTLE_FG))
        blocks.append(self._series_actions(item, server, route["item_id"]))
        if item.get("Overview"):
            blocks.append(self._paragraph(item["Overview"], 18, self._body_w(w)))
        seasons = data.get("seasons") or []
        if seasons:
            blocks.append(self._tile_row(
                _("Seasons"), seasons, "series-seasons"))
        people_row = self._people_row(item.get("People") or [], server)
        if people_row is not None:
            blocks.append(people_row)
        if data.get("similar"):
            blocks.append(self._tile_row(
                _("More Like This"), data["similar"], "series-similar"))
        return VScroll(Column(blocks, pad=16, gap=16), id="series", flex=1)

    def _render_season(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        episodes = data.get("episodes") or []
        seasons = data.get("seasons") or []
        server = route.get("server") or self.server
        geom = self.geom_wide   # episodes are landscape Thumb cards
        season_item = next((s for s in seasons
                            if s.get("Id") == route["item_id"]), {})
        title_row = [Text(route.get("title", ""), size=26, bold=True)]
        if len(seasons) > 1:
            names = [s.get("Name", "") for s in seasons]
            cur = next((i for i, s in enumerate(seasons)
                        if s.get("Id") == route["item_id"]), 0)
            title_row.append(Dropdown(
                "season-switch", names, selected=cur, w=220,
                on_select=lambda i, v: self._switch_season(route, seasons[i])))
        if route.get("series_id"):
            title_row.append(self._action_btn(
                "movie", _("To Series"), "season-to-series",
                lambda: self.navigate({
                    "kind": "series", "server": server,
                    "item_id": route["series_id"],
                    "title": season_item.get("SeriesName", "")})))
        header = [Row(title_row, gap=12, align="center"),
                  Row(self._common_actions(season_item or {"Id": route["item_id"],
                                           "Type": "Season"}, server, "se"),
                      gap=8, align="center")]
        rows = header + self._grid_of(
            episodes, "ep", size, geom=geom, image_type="Thumb",
            scroll_id="season", head_h=100)
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=self.GRID_GAP),
                       id="season", flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "season", off, mx))

    def _switch_season(self, route, season):
        self.navigate({
            "kind": "season",
            "server": route.get("server") or self.server,
            "item_id": season.get("Id"),
            "series_id": route.get("series_id"),
            "title": season.get("Name", ""),
        })

    def _search(self, term):
        term = (term or "").strip()
        if not term:
            return
        self.navigate({"kind": "search", "server": self.server,
                       "term": term, "title": _("Search")})

    def _render_search(self, route, size):
        term = route.get("term", "")
        if not term:
            return self._error(_("Type in the search box above."))
        data = route.get("_data")
        if data is None:
            return self._busy()
        items = data.get("items") or []
        people = data.get("people") or []
        rows = [Text(_('Results for "%s"') % term, size=24, bold=True)]
        if people:
            rows.append(self._tile_row(_("People"), people, "search-people",
                                       geom=self.geom_square))
        # Group by type, each with its natural tile shape (like the Tk browser).
        groups = [
            (_("Movies"), ("Movie",), self.geom, "Primary"),
            (_("Shows"), ("Series",), self.geom, "Primary"),
            (_("Episodes"), ("Episode",), self.geom_wide, "Thumb"),
            (_("Videos"), ("Video", "MusicVideo"), self.geom_wide, "Primary"),
            (_("Albums"), ("MusicAlbum",), self.geom_square, "Primary"),
            (_("Artists"), ("MusicArtist",), self.geom_square, "Primary"),
        ]
        used = set()
        for label, types_, geom, itype in groups:
            group = [it for it in items if it.get("Type") in types_]
            if group:
                used.update(types_)
                rows.append(self._tile_row(
                    label, group, "search-" + label, geom=geom,
                    image_type=itype))
        songs = [it for it in items if it.get("Type") == "Audio"]
        if songs:
            server = route.get("server") or self.server
            ids = [s.get("Id") for s in songs]
            rows.append(Text(_("Songs"), size=24, bold=True))
            rows.append(self._track_list(
                songs, "search-song",
                lambda i: self._play_list(ids, server, i, audio=True),
                scroll_id="search", head_h=120))
        other = [it for it in items
                 if it.get("Type") not in used and it.get("Type") != "Audio"]
        if other:
            rows.append(self._tile_row(_("Other"), other, "search-other"))
        if not items and not people:
            rows.append(Text(_("No results."), size=18, color=theme.SUBTLE_FG))
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=12,
                              align="stretch"), id="search", flex=1)

    # ---------------------------------------- route loaders

    def _load_home(self, route, ep):
        def work():
            server = route.get("server") or self.server
            libs = self.source.get_libraries(server)
            rows = self.source.get_home_rows(server, libs)
            return {"libraries": libs, "rows": rows}
        self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)

    def _load_grid(self, route, ep):
        srv = route.get("server") or self.server
        parent = route["parent_id"]
        _n, sort_by, sort_order = SORTS[route.get("_sort", 0)]
        filters = route.get("_filters") or {}

        collections = bool(route.get("_collections"))

        def work():
            if collections:
                # Collections are server-wide and recursive (a BoxSet
                # can gather items from several libraries), so this is a
                # different query, not a filter on the library.
                items, total = self.source.get_movie_collections(
                    srv, sort_by=sort_by, sort_order=sort_order,
                    filters=filters)
            else:
                items, total = self.source.get_library_items(
                    srv, parent, sort_by=sort_by, sort_order=sort_order,
                    filters=filters)
            vals = route.get("_filtervals")
            if vals is None:
                try:
                    vals = self.source.get_filter_values(srv, parent)
                except Exception:
                    vals = {"genres": [], "years": []}
            return items, total, vals

        def done(res):
            route["_items"], route["_total"], route["_filtervals"] = res
            # The toggle only makes sense on a movies library, and only
            # when the source can answer it (the offline catalog can't).
            route["_collection_capable"] = (
                route.get("collection_type") == "movies"
                and hasattr(self.source, "get_movie_collections"))
        self._route_async(route, work, done, ep)

    def _load_detail(self, route, ep):
        srv = route.get("server") or self.server
        iid = route["item_id"]

        def work():
            item = self.source.get_item(srv, iid)
            similar = []
            try:
                similar = self.source.get_similar(srv, iid)
            except Exception:
                pass
            trailers = []
            if (item or {}).get("Type") in ("Movie", "Series"):
                try:
                    trailers = self.source.get_trailers(srv, iid) or []
                except Exception:
                    pass  # older servers / no trailers: just no button
            return {"item": item, "similar": similar,
                    "trailers": trailers}
        self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)

    def _load_series(self, route, ep):
        srv = route.get("server") or self.server
        iid = route["item_id"]

        def work():
            similar = []
            try:
                similar = self.source.get_similar(srv, iid) or []
            except Exception:
                pass   # offline / older server: just no row
            return {
                "item": self.source.get_item(srv, iid),
                "seasons": self.source.get_seasons(srv, iid),
                "similar": similar,
            }
        self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)

    def _load_season(self, route, ep):
        srv = route.get("server") or self.server

        def work():
            return {
                "episodes": self.source.get_episodes(
                    srv, route.get("series_id"), route["item_id"]),
                "seasons": self.source.get_seasons(
                    srv, route.get("series_id")),
            }
        self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)

    def _load_search(self, route, ep):
        srv = route.get("server") or self.server
        term = route.get("term", "")

        def work():
            if not term:
                return {"items": [], "people": []}
            items = self.source.search(srv, term)
            people = []
            try:
                people = self.source.search_people(srv, term)
            except Exception:
                pass
            return {"items": items, "people": people}
        self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)

    def _load_person(self, route, ep):
        srv = route.get("server") or self.server

        def work():
            return self.source.get_person_items(srv, route["person_id"])

        def done(res):
            route["_items"], route["_total"] = res
        self._route_async(route, work, done, ep)
