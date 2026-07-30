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
from ...mpvtk.widgets import Box, Column, Dropdown, Row, Text, VScroll
from .. import components, theme
from ..components import chrome, controls, detail as detail_components
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
        if isinstance(banner, Box):
            # No artwork (or still loading): draw the heading normally, with
            # the same title/context split the baked one uses.
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
        if item.get("Type") == "Episode" and item.get("SeriesId"):
            btns.append(controls.action_btn(
                "movie", _("Go to Series"), "act-series",
                lambda: self.ctx.nav.navigate({
                    "kind": "series", "server": server,
                    "item_id": item["SeriesId"],
                    "title": item.get("SeriesName", "")})))
        return Row(btns, gap=8, align="center")

    def _play_buttons(self, item, server, trailers=None):
        actions = self.ctx.actions
        route = self.route
        ud = item.get("UserData") or {}
        pos = ud.get("PlaybackPositionTicks") or 0
        srcid = (route.get("_srcid")
                 or ((item.get("MediaSources") or [{}])[0]).get("Id"))
        aid, sid = self._effective_tracks(item)
        buttons = []
        if pos > 0:
            buttons.append(controls.action_btn(
                "play_arrow",
                _("Resume") + "  " + detail_components.fmt_ticks(pos),
                "btn-resume",
                lambda: actions.play(item, server, offset_ticks=pos,
                                     srcid=srcid, aid=aid, sid=sid),
                primary=True, size=18))
        buttons.append(controls.action_btn(
            "play_arrow", _("Play"), "btn-play",
            lambda: actions.play(item, server, srcid=srcid, aid=aid, sid=sid),
            primary=(pos <= 0), size=18))
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
        route = self.route
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
        # detail view's own play for exactly that reason.
        srcid = (route.get("_srcid")
                 or ((item.get("MediaSources") or [{}])[0]).get("Id"))
        aid, sid = self._effective_tracks(item)
        return art.tiles.tile_row(
            _("Scenes"), scene_tiles, "detail-scenes", geom=geom,
            on_click=lambda t: self.ctx.actions.play(
                item, server, offset_ticks=t.get("_start_ticks") or 0,
                srcid=srcid, aid=aid, sid=sid))

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

    @staticmethod
    def _picker_row(label, node_id, names, selected, on_select):
        return Row([Text(label, w=90, size=16, color=theme.SUBTLE_FG),
                    Dropdown(node_id, names, selected=selected, w=300,
                             on_select=on_select)], gap=8, align="center")

    # -- media info --------------------------------------------------------

    def _media_info_line(self, item):
        """Codec/resolution/audio/size line plus "Ends at", like
        jellyfin-web — enough to judge direct-play before hitting Play."""
        src = self._sel_source(item.get("MediaSources") or [])
        streams = (src or {}).get("MediaStreams") or []
        parts = []
        video = next((s for s in streams if s.get("Type") == "Video"), None)
        if video:
            if video.get("DisplayTitle"):
                parts.append(video["DisplayTitle"])
            else:
                # Codec as well as resolution. "1080p" alone drops the one
                # thing that decides whether it will direct-play; Tk showed
                # both when the server had no DisplayTitle to give.
                bits = [(video.get("Codec") or "").upper()]
                if video.get("Width") and video.get("Height"):
                    bits.append("%dx%d" % (video["Width"], video["Height"]))
                elif video.get("Height"):
                    bits.append("%dp" % video["Height"])
                joined = " ".join(b for b in bits if b)
                if joined:
                    parts.append(joined)
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
            parts.append(components.human_size(src["Size"]))
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
