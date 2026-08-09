"""The item detail screen — a movie, an episode, a video.

The largest conversion so far, and the one that shows what the two prep
steps bought: it needs the tile builders, the chrome, the action buttons and
the playback launcher, and reaches ``ctx.shell`` for none of them.

Everything moved here was private to this screen — the version/audio/subtitle
pickers, the media-info line, the play buttons, the chapter carousel. They
were methods on the browser only because there was nowhere else to put them.
"""

import datetime
import logging

from ...i18n import _
from ...mpvtk.scaling import px
from ...mpvtk.widgets import Column, Dropdown, Row, Text, VScroll
from .. import components, theme
from ..components import (chrome, controls, detail as detail_components,
                          media_info)
from .base import Page

log = logging.getLogger("mpvtk_browser.pages.detail")


class DetailPage(Page):
    kind = "detail"

    # -- load --------------------------------------------------------------

    def load(self, epoch):
        route = self.route
        source = self.ctx.source
        srv = route.get("server") or self.ctx.server
        iid = route["item_id"]

        def work():
            item = source.get_item(srv, iid)
            similar = []
            try:
                similar = source.get_similar(srv, iid)
            except Exception:
                pass
            trailers: list = []
            if (item or {}).get("Type") in ("Movie", "Series"):
                try:
                    trailers = source.get_trailers(srv, iid) or []
                except Exception:
                    pass  # older servers / no trailers: just no button
            return {"item": item, "similar": similar, "trailers": trailers}

        self.route_async(work, lambda d: route.__setitem__("_data", d), epoch)

    # -- render ------------------------------------------------------------

    def render(self, size):
        art = self.ctx.art
        tiles = art.tiles
        route = self.route
        data = route.get("_data")
        if data is None:
            return chrome.busy()
        item = data.get("item")
        if not item:
            return chrome.error(_("Item not available."))
        w = size[0]
        server = route.get("server") or self.ctx.server
        bw, bh = tiles.banner_box(w)
        title, context = components.heading_for(item)
        meta = detail_components.meta_line(item)
        banner = tiles.backdrop_node(item, (bw, bh), "detail-bd",
                                     title=title, meta=meta, context=context)
        blocks = [banner]
        if not tiles.header_bakes_heading(item):
            # No artwork *at all* — asked of the DTO, not of the node that
            # came back. The node cannot answer it: a placeholder means
            # either "none" or "not yet", and drawing the heading below the
            # banner in the second case moved everything under it (these
            # blocks' whole height, play buttons included) the moment the
            # image landed. A header that will get artwork bakes its heading
            # into the banner in both states; see `backdrop_node`.
            if context:
                blocks.append(Text(context, size=17, color=theme.SUBTLE_FG))
            blocks.append(Text(title, size=26, bold=True, wrap=True,
                               w=tiles.body_w(w)))
            if meta:
                blocks.append(Text(meta, size=18, color=theme.SUBTLE_FG))
        info = self._media_info_line(item)
        if info:
            blocks.append(Text(info, size=15, color=theme.SUBTLE_FG))
        blocks.append(self._play_buttons(item, server,
                                         trailers=data.get("trailers")))
        blocks.append(self._detail_actions(item, server))
        blocks.extend(self._track_pickers(item))
        if item.get("Overview"):
            blocks.append(chrome.paragraph(item["Overview"], 18,
                                           tiles.body_w(w)))
        scenes = self._scenes_row(item, server)
        if scenes is not None:
            blocks.append(scenes)
        people = detail_components.people_row(tiles, item.get("People") or [])
        if people is not None:
            blocks.append(people)
        if data.get("similar"):
            # Shaped by its own artwork, like jellyfin-web
            # (shape: autooverflow, itemDetails/index.js:1245).
            # "More Like This" for a film is posters and for a
            # music album is square covers; one fixed poster row
            # cropped the second case. Poster stays the fallback
            # for a row where nothing carries an aspect ratio.
            sim_geom, sim_type = tiles.auto_geom(
                data["similar"], default=tiles.art.geom,
                default_type="Primary")
            blocks.append(tiles.tile_row(
                _("More Like This"), data["similar"], "detail-similar",
                geom=sim_geom, image_type=sim_type))
        return VScroll(Column(blocks, pad=16, gap=16), id="detail", flex=1,
                       offset=self.parked_scroll("detail"))

    # -- actions -----------------------------------------------------------

    def _detail_actions(self, item, server):
        btns = detail_components.common_actions(
            self.ctx.actions, self.ctx.art.tiles, item, server, "act")
        if item.get("MediaSources"):
            # Here the DTO really does carry them (DETAIL_FIELDS), so this
            # asks jellyfin-web's own question rather than the tile menu's
            # type-shaped stand-in for it. An item whose sources came back
            # empty has nothing to show, and a button that opens an empty
            # dialog is worse than no button.
            btns.append(controls.action_btn(
                "info", _("Media Info"), "act-minfo",
                lambda: self.ctx.dialogs.media_info(item, server)))
        # `actions.offline` as well as CanDelete, like the tile menu: the
        # flag comes off a DTO the *catalog* stored, so offline it can still
        # say True -- and pressing it would reach for a server that is not
        # there. The local copy has its own Remove Download.
        if not self.ctx.actions.offline and self.ctx.actions.can_delete(item):
            # Last in the row, like the tile menu, and for the same reason:
            # it is the only control here that destroys anything.
            btns.append(controls.action_btn(
                "delete", _("Delete from Disk"), "act-delete",
                lambda: self.ctx.actions.confirm_delete_item(
                    item, server, on_done=self._left_after_delete)))
        if item.get("Type") == "Episode" and item.get("SeriesId"):
            btns.append(controls.action_btn(
                "movie", _("Go to Series"), "act-series",
                lambda: self.ctx.nav.navigate({
                    "kind": "series", "server": server,
                    "item_id": item["SeriesId"],
                    "title": item.get("SeriesName", "")})))
        return Row(btns, gap=8, align="center")

    def _left_after_delete(self):
        """Leave the page once its item is gone.

        Unlike a grid, this screen *is* the deleted item: re-reading it
        would fetch a 404 and show an error where a film used to be.

        The flag is what makes the list underneath re-read. Going back
        alone does **not**: `_land_back` refreshes Home and the two cases
        that had a reason to, and a grid otherwise keeps the items it was
        loaded with -- so the deleted tile was still sitting there, and
        pressing it 404s. Found by hand-testing; the unit tests only ever
        checked that we left the page, which was the easy half.
        """
        self.route["_deleted"] = True
        self.ctx.nav.go_back()

    def _start(self, item, server, offset_ticks=None):
        """Play `item` with the version and tracks selected **now**.

        Read when the button is pressed rather than when it was built. The
        track pickers write their choice straight to the route and force no
        repaint -- nothing drawn depends on them, and the dropdown shows its
        own selection -- so a pair captured at build time is the selection as
        of the last time this page happened to draw. That is the *previous*
        choice, which is what plays, unless some unrelated repaint (a
        thumbnail landing, a websocket item update) rebuilt the closure in
        between. Hence a bug that came and went.

        The version picker escaped it only because `_pick_source` invalidates
        for its own reasons -- the streams shown have to change with it.
        """
        srcid = (self.route.get("_srcid")
                 or ((item.get("MediaSources") or [{}])[0]).get("Id"))
        aid, sid = self._effective_tracks(item)
        self.ctx.actions.play(item, server, offset_ticks=offset_ticks,
                              srcid=srcid, aid=aid, sid=sid)

    def _play_buttons(self, item, server, trailers=None):
        actions = self.ctx.actions
        ud = item.get("UserData") or {}
        pos = ud.get("PlaybackPositionTicks") or 0
        buttons = []
        # Opened from a remote or the arrow keys, this page lands focused on
        # whichever of the two is the call to action -- Resume when there is
        # a position to resume from, Play otherwise. Watching something is
        # the reason the page was opened, and without this the first press
        # of any arrow key had to hunt for it from wherever focus last was.
        if pos > 0:
            buttons.append(controls.action_btn(
                "play_arrow",
                _("Resume") + "  " + detail_components.fmt_ticks(pos),
                "btn-resume",
                lambda: self._start(item, server, offset_ticks=pos),
                primary=True, size=18, autofocus=True))
        buttons.append(controls.action_btn(
            "play_arrow", _("Play"), "btn-play",
            lambda: self._start(item, server),
            primary=(pos <= 0), size=18, autofocus=(pos <= 0)))
        tids = [t.get("Id") for t in (trailers or []) if t.get("Id")]
        if tids:
            buttons.append(controls.action_btn(
                "movie", _("Trailer"), "btn-trailer",
                lambda: actions.play_list(tids, server, 0), size=18))
        return Row(buttons, gap=10)

    def _scenes_row(self, item, server):
        """The chapter carousel ("Scenes"), each tile seeking to its start.

        Chapter art is indexed rather than tagged, so the tiles carry a
        ready-made image spec+url (see TileRenderer.poster_for) — image_spec
        can't address it."""
        art = self.ctx.art
        chapters = item.get("Chapters") or []
        if len(chapters) < 2:
            return None          # a single chapter is just the start
        iid = item.get("Id")
        # A chapter thumbnail is a frame of the video, so the tile has to be
        # the video's shape, not 16:9 by assumption -- a 4:3 or portrait
        # source letterboxed inside a landscape card. jellyfin-web reads the
        # first video stream and goes square at <= 1.2
        # (chaptercardbuilder.js:30-39); same rule, same threshold.
        streams = ((item.get("MediaSources") or [{}])[0]
                   .get("MediaStreams") or [])
        video = next((s for s in streams if s.get("Type") == "Video"), {})
        vw, vh = video.get("Width"), video.get("Height")
        geom = (art.geom_square if vw and vh and (vw / vh) <= 1.2
                else art.geom_wide)
        scene_tiles = []
        for i, ch in enumerate(chapters):
            url = None
            try:
                # Physical: geom is logical, and poster_for keys/decodes
                # this pseudo-item at raster(tile_w, tile_h). PIL's
                # thumbnail() only ever downscales, so a logical request
                # here leaves the art stranded at 1x inside a scaled card.
                url = self.ctx.source.chapter_image_url(
                    server, iid, i, ch, width=px(geom.tile_w))
            except Exception:
                log.debug("chapter art failed", exc_info=True)
            start = ch.get("StartPositionTicks") or 0
            scene_tiles.append({
                "Id": "%s#ch%d" % (iid, i),
                "Name": ch.get("Name") or _("Chapter %d") % (i + 1),
                "Type": "Chapter",
                "_start_ticks": start,
                "_subtitle": detail_components.fmt_ticks(start),
                "_image_spec": ((iid, "Chapter%d" % i,
                                 ch.get("ImageTag") or "none")
                                if url else None),
                "_image_url": url,
            })
        # Starting at a chapter has to carry the same version and tracks
        # the Play button would — Tk's chapter click routes through the
        # detail view's own play for exactly that reason. Through _start for
        # the rest of it: read at click time, so a chapter started after a
        # track pick gets the pick rather than what was showing when the row
        # was built.
        return art.tiles.tile_row(
            _("Scenes"), scene_tiles, "detail-scenes", geom=geom,
            on_click=lambda t: self._start(
                item, server, offset_ticks=t.get("_start_ticks") or 0))

    # -- version / track selection ----------------------------------------

    def _sel_source(self, sources):
        if not sources:
            return None
        return next((s for s in sources
                     if s.get("Id") == self.route.get("_srcid")), sources[0])

    def _pick_source(self, src):
        self.route["_srcid"] = src.get("Id")
        self.route["_aid"] = None   # let the new version pick its own defaults
        self.route["_sid"] = None
        self.ctx.invalidate()

    def _default_track_indices(self, src, item):
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
        cache = self.route.setdefault("_def_tracks", {})
        key = (src or {}).get("Id")
        if key in cache:
            return cache[key]
        aid = sid = None
        if src:
            try:
                from ...conf import settings
                from ...language_config import apply as apply_language_config

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

    def _effective_tracks(self, item):
        """``(aid, sid)`` the pickers display and playback is started with:
        the user's pick where they made one, otherwise the resolved default.

        Both are sent, not just the one that was touched — mirroring the Tk
        browser, whose comboboxes are always populated. Sending only the
        touched one marks the play explicit and map_streams then returns
        before defaulting the other, which is how picking an audio track
        silently turned the subtitles off."""
        route = self.route
        src = self._sel_source(item.get("MediaSources") or [])
        streams = (src or {}).get("MediaStreams") or []
        def_aid, def_sid = self._default_track_indices(src, item)
        aid, sid = route.get("_aid"), route.get("_sid")
        # Only default a kind that actually has streams, so an item with no
        # subtitles isn't reported as a deliberate choice.
        if aid is None and any(s.get("Type") == "Audio" for s in streams):
            aid = def_aid
        if sid is None and any(s.get("Type") == "Subtitle" for s in streams):
            sid = def_sid
        return aid, sid

    def _track_pickers(self, item):
        route = self.route
        sources = item.get("MediaSources") or []
        rows = []
        if len(sources) > 1:
            # Two sources with the same Name gave two indistinguishable
            # dropdown rows — you could not tell which one you were picking.
            # Tk suffixed the duplicate with its position.
            names, seen = [], set()
            for i, src in enumerate(sources):
                label_text = src.get("Name") or _("Version %d") % (i + 1)
                if label_text in seen:
                    label_text = "%s (%d)" % (label_text, i + 1)
                seen.add(label_text)
                names.append(label_text)
            cur = next((i for i, s in enumerate(sources)
                        if s.get("Id") == route.get("_srcid")), 0)
            rows.append(self._picker_row(
                _("Version"), "dt-version", names, cur,
                lambda i, v: self._pick_source(sources[i])))
        src = self._sel_source(sources)
        streams = (src or {}).get("MediaStreams") or []
        audio = [s for s in streams if s.get("Type") == "Audio"]
        subs = [s for s in streams if s.get("Type") == "Subtitle"]

        def label(s, kind):
            return (s.get("DisplayTitle") or s.get("Language")
                    or "%s %s" % (kind, s.get("Index")))
        # What the pickers show must be what will play — see _effective_tracks.
        eff_aid, eff_sid = self._effective_tracks(item)
        if audio:
            names = [label(s, _("Audio")) for s in audio]
            cur = next((i for i, s in enumerate(audio)
                        if s.get("Index") == eff_aid), 0)
            rows.append(self._picker_row(
                _("Audio"), "dt-audio", names, cur,
                lambda i, v: route.__setitem__("_aid", audio[i].get("Index"))))
        if subs:
            names = [_("None")] + [label(s, _("Sub")) for s in subs]
            cur = 0
            if eff_sid not in (None, -1):
                cur = next((i + 1 for i, s in enumerate(subs)
                            if s.get("Index") == eff_sid), 0)
            rows.append(self._picker_row(
                _("Subtitle"), "dt-sub", names, cur,
                lambda i, v: route.__setitem__(
                    "_sid", -1 if i == 0 else subs[i - 1].get("Index"))))
        return rows

    #: How wide the OPEN list of a track picker may get. The control stays
    #: 300 either way -- see below.
    PICKER_POPUP_W = 640

    @staticmethod
    def _picker_row(label, node_id, names, selected, on_select):
        """One `Label  [dropdown]` row for a version / audio / subtitle
        picker.

        ``popup_w``, exactly as the Settings page's audio-device list uses
        it (**[iw]**), and for the same reason: these three carry the
        longest text in the app and **none of it is ours**. A track's
        DisplayTitle comes from whoever made the file -- "Signs & Songs -
        English - SUBRIP", "Surround 5.1 - English - DTS-HD MA - Default" --
        and at 300px every row of it ellipsizes to the same prefix, so the
        picker offers a choice it cannot show. Version names are the same:
        they are the user's own directory or edition labels.

        The OPEN list widens, not the control. A control wider than 300
        would put these rows out of line with everything else on the page,
        and it is closed almost all of the time; the popup takes only as
        much of the allowance as its widest item needs.
        """
        return Row([Text(label, w=90, size=16, color=theme.SUBTLE_FG),
                    Dropdown(node_id, names, selected=selected, w=300,
                             popup_w=DetailPage.PICKER_POPUP_W,
                             on_select=on_select)], gap=8, align="center")

    # -- media info --------------------------------------------------------

    def _media_info_line(self, item):
        """Codec/resolution/audio/size line plus "Ends at", like
        jellyfin-web — enough to judge direct-play before hitting Play.

        The file's half of this is ``media_info.summary_parts``, shared with
        the two screens that show the same knowledge in full; "Ends at" stays
        here because it is a property of the *item* (its resume position),
        not of the file, and neither of those screens wants it.
        """
        src = self._sel_source(item.get("MediaSources") or [])
        parts = media_info.summary_parts(src)
        runtime = item.get("RunTimeTicks")
        if runtime:
            pos = (item.get("UserData") or {}).get(
                "PlaybackPositionTicks") or 0
            remaining = max(runtime - pos, 0) // 10000000
            ends = (datetime.datetime.now()
                    + datetime.timedelta(seconds=remaining))
            parts.append(_("Ends at %s") % ends.strftime("%H:%M"))
        return "   ·   ".join(p for p in parts if p)
