"""Screens for the library browser: Home, Grid, Series, Season, Detail, Search.

Each view is built fresh on navigation. Views fetch data off the UI thread via
``app.run_async`` and render on completion, so the window never blocks on the
network.
"""

import json
import logging

from ..i18n import _
from ..constants import USER_APP_NAME
from ..utils import get_sub_display_title, get_resource
from ..language_config import apply as apply_language_config, parse_language_config
from ..sync.db import (SyncDB, STATUS_COMPLETE, STATUS_DOWNLOADING,
                       STATUS_PENDING, STATUS_ERROR)
from .theme import CARD_BG, TEXT_FG, SUBTLE_FG, WINDOW_BG, ENTRY_BG, PANEL_BG
from .widgets import (
    ScrollableGrid, HScrollRow, VScrollFrame, format_ticks, make_key, human_size,
    is_watched,
)

log = logging.getLogger("library_browser.views")

HEADER_H = 260
HEADER_W = 1100


def poster_box(width):
    return (width, int(width * 1.5))


def thumb_box(width):
    return (width, int(width * 9 / 16))


def build_media_header(app, parent, item, title_text=None):
    """A backdrop banner with the item's title overlaid. Returns the canvas."""
    tk = app.tk
    header = tk.Frame(parent, bg=WINDOW_BG, height=HEADER_H)
    header.pack(fill="x")
    header.pack_propagate(False)
    canvas = tk.Canvas(header, bg=WINDOW_BG, highlightthickness=0, bd=0,
                       height=HEADER_H)
    canvas.pack(fill="both", expand=True)

    url = app.source.backdrop_url(app.current_server, item, width=HEADER_W,
                                  height=HEADER_H, fill=True)
    if url:
        key = make_key(item.get("Id"), "Backdrop", "hdr", HEADER_W, HEADER_H)

        def on_img(photo):
            try:
                canvas.create_image(0, 0, anchor="nw", image=photo, tags="bg")
                canvas.tag_lower("bg")
                canvas._bg_ref = photo
            except Exception:
                pass

        app.thumbs.request(key, url, (HEADER_W, HEADER_H), on_img)

    title = title_text if title_text is not None else item.get("Name", "")
    # Shadow + text for legibility over arbitrary art.
    canvas.create_text(22, HEADER_H - 38, anchor="w", text=title, fill="#000000",
                       font=("TkDefaultFont", 17, "bold"))
    canvas.create_text(20, HEADER_H - 40, anchor="w", text=title, fill="#ffffff",
                       font=("TkDefaultFont", 17, "bold"))
    return canvas


def metadata_line(item):
    meta = []
    if item.get("ProductionYear"):
        meta.append(str(item["ProductionYear"]))
    if item.get("OfficialRating"):
        meta.append(item["OfficialRating"])
    if item.get("RunTimeTicks"):
        meta.append(format_ticks(item["RunTimeTicks"]))
    if item.get("CommunityRating"):
        meta.append("★ %.1f" % item["CommunityRating"])
    genres = ", ".join(item.get("Genres", [])[:3])
    if genres:
        meta.append(genres)
    return "   ".join(meta)


class BaseView:
    def __init__(self, app, route):
        self.app = app
        self.route = route
        self.frame = None

    def build(self, parent):
        self.frame = self.app.tk.Frame(parent, bg=CARD_BG)
        self._build()
        return self.frame

    def _build(self):
        raise NotImplementedError

    def _spinner(self, parent=None):
        parent = parent or self.frame
        lbl = self.app.tk.Label(parent, text=_("Loading…"), bg=CARD_BG,
                                fg=SUBTLE_FG)
        lbl.pack(pady=40)
        return lbl

    def _error(self, parent, exc):
        msg = _("Could not load content:") + "\n" + str(exc)
        self.app.tk.Label(parent, text=msg, bg=CARD_BG, fg="#e57373",
                          justify="center").pack(pady=40)


class HomeView(BaseView):
    """Home screen with stale-while-revalidate caching.

    On navigation back to Home we render the cached data instantly, then refetch
    in the background and re-render only if it changed.
    """

    @staticmethod
    def _signature(result):
        """A stable fingerprint of what's *shown*, ignoring volatile fields
        (resume %, play counts) so an unchanged home doesn't trigger a redraw."""
        libraries, rows = result
        return (
            tuple((lib.get("Id"), lib.get("Name")) for lib in libraries),
            tuple((row["title"], tuple(i.get("Id") for i in row["items"]))
                  for row in rows),
        )

    def _build(self):
        self.server = self.app.current_server
        self.spinner = None
        self.rendered = False
        self.sig = None

        cached = self.app.home_cache.get(self.server)
        if cached is not None:
            self._render_home(cached)
            self.sig = self._signature(cached)
            self.rendered = True
        else:
            self.spinner = self._spinner()
        self._refresh()

    def _refresh(self):
        server = self.server

        def work():
            return (self.app.source.get_libraries(server),
                    self.app.source.get_home_rows(server))

        def done(result):
            self.app.home_cache[server] = result
            new_sig = self._signature(result)
            # Same items as what's already on screen — don't flicker/lose scroll.
            if self.rendered and new_sig == self.sig:
                return
            if self.spinner is not None:
                self.spinner.destroy()
                self.spinner = None
            for child in self.frame.winfo_children():
                child.destroy()
            self._render_home(result)
            self.sig = new_sig
            self.rendered = True

        def fail(e):
            if self.spinner is not None:
                self.spinner.destroy()
                self.spinner = None
            # Server unreachable with downloads available → fall back to offline.
            if not self.app.is_offline and self.app.sync_items:
                self.app.offline_fallback()
            elif not self.rendered:
                self._error(self.frame, e)

        self.app.run_async(work, done, fail)

    def _render_home(self, result):
        libraries, rows = result
        server = self.server
        container = VScrollFrame(self.app, self.frame)
        container.widget().pack(fill="both", expand=True)
        body = container.body()

        if libraries:
            # Libraries read better as landscape cards (like the web client).
            lib_row = HScrollRow(self.app, body, _("Libraries"),
                                 thumb_box(int(self.app.image_width * 1.4)))
            lib_row.widget().pack(fill="x")
            lib_row.set_items(libraries, server, image_type="Primary",
                              on_click=self.app.open_item, subtitle_fn=lambda i: "")

        for row in rows:
            if any(i.get("Type") == "Episode" for i in row["items"]):
                box = thumb_box(int(self.app.image_width * 1.4))
                image_type = "Thumb"
            else:
                box = poster_box(self.app.image_width)
                image_type = "Primary"
            hrow = HScrollRow(self.app, body, row["title"], box)
            hrow.widget().pack(fill="x")
            hrow.set_items(row["items"], server, image_type=image_type,
                           on_click=self.app.open_item)

        if not libraries and not rows:
            self.app.tk.Label(body, text=_("This server has no video libraries."),
                              bg=CARD_BG, fg=SUBTLE_FG).pack(pady=40)


SORTS = [
    (_("Name"), "SortName", "Ascending"),
    (_("Date Added"), "DateCreated", "Descending"),
    (_("Release Date"), "PremiereDate", "Descending"),
    (_("Community Rating"), "CommunityRating", "Descending"),
    (_("Random"), "Random", "Ascending"),
]


class GridView(BaseView):
    """Infinite-scrolling grid of the children of a library/folder."""

    def __init__(self, app, route):
        super().__init__(app, route)
        self.sort_idx = route.get("sort_idx", 0)
        self.grid = None
        self.status = None
        self.total = None
        self.loaded = 0
        self.loading = False
        self._first = True

    def _build(self):
        tk, ttk = self.app.tk, self.app.ttk
        bar = tk.Frame(self.frame, bg=CARD_BG)
        bar.pack(fill="x", padx=8, pady=4)
        tk.Label(bar, text=self.route.get("title", ""), bg=CARD_BG, fg=TEXT_FG,
                 font=("TkDefaultFont", 14, "bold")).pack(side="left")

        self.sort_var = tk.StringVar(value=SORTS[self.sort_idx][0])
        sort_box = ttk.Combobox(bar, textvariable=self.sort_var, state="readonly",
                                width=18, values=[s[0] for s in SORTS])
        sort_box.pack(side="right")
        sort_box.bind("<<ComboboxSelected>>", self._on_sort)
        tk.Label(bar, text=_("Sort:"), bg=CARD_BG, fg=SUBTLE_FG).pack(
            side="right", padx=(0, 4))

        self.grid = ScrollableGrid(self.app, self.frame,
                                   poster_box(self.app.image_width))
        self.grid.widget().pack(fill="both", expand=True)
        self.grid.on_near_end = self._load_more

        self.status = tk.Label(self.frame, text="", bg=CARD_BG, fg=SUBTLE_FG,
                               anchor="w")
        self.status.pack(fill="x", padx=8, pady=2)

        self._reset_and_load()

    def _on_sort(self, _e):
        self.sort_idx = [s[0] for s in SORTS].index(self.sort_var.get())
        self._reset_and_load()

    def _reset_and_load(self):
        self.total = None
        self.loaded = 0
        self.loading = False
        self._first = True
        self._load_more()

    def _load_more(self):
        if self.loading:
            return
        if self.total is not None and self.loaded >= self.total:
            return
        self.loading = True
        self.status.config(text=_("Loading…"))
        server = self.app.current_server
        _name, sort_by, order = SORTS[self.sort_idx]
        start = self.loaded

        def work():
            return self.app.source.get_library_items(
                server, self.route["parent_id"], sort_by=sort_by,
                sort_order=order, start_index=start, limit=self.app.page_size)

        def done(result):
            items, total = result
            self.total = total
            self.loaded += len(items)
            # An empty page while we still think there's more would otherwise
            # re-arm near-end and request the same page forever — stop here.
            if not items and (total is None or self.loaded < total):
                self.total = self.loaded
            # Random reshuffles per request, so a second page would repeat/skip
            # items — cap it to the first page.
            if SORTS[self.sort_idx][1] == "Random":
                self.total = self.loaded
            if self._first:
                self.grid.set_items(items, server, image_type="Primary",
                                    on_click=self.app.open_item)
                self._first = False
            else:
                self.grid.append_items(items)
            self.loading = False
            if total:
                self.status.config(text=_("%d of %d") % (self.loaded, total))
            else:
                self.status.config(text=_("Nothing here."))

        def fail(e):
            self.loading = False
            self.status.config(text=_("Failed to load."))
            log.warning("Grid load failed: %s", e)

        self.app.run_async(work, done, fail)


class SeriesView(BaseView):
    """Series overview: backdrop, metadata, overview, and a row of seasons."""

    def _build(self):
        server = self.app.current_server
        sid = self.route["series_id"]
        spinner = self._spinner()

        def work():
            return (self.app.source.get_item(server, sid),
                    self.app.source.get_seasons(server, sid))

        def done(result):
            spinner.destroy()
            item, seasons = result
            container = VScrollFrame(self.app, self.frame)
            container.widget().pack(fill="both", expand=True)
            body = container.body()

            if item:
                build_media_header(self.app, body, item)
                actions = self.app.tk.Frame(body, bg=CARD_BG)
                actions.pack(fill="x", padx=16, pady=(10, 0))
                self.app.ttk.Button(
                    actions, text=_("▶ Play Next Up"), style="Accent.TButton",
                    command=lambda: self.app.play_next_up(sid)).pack(side="left")
                wlabel = (_("Mark unwatched") if is_watched(item)
                          else _("Mark watched"))
                self.app.ttk.Button(
                    actions, text=wlabel,
                    command=lambda: self.app.set_watched(
                        server, sid, not is_watched(item), refresh=True)
                    ).pack(side="left", padx=8)
                if sid in self.app.sync_series:
                    self.app.ttk.Button(
                        actions, text=_("🗑 Remove downloads"),
                        command=lambda: self.app.delete_download(series_id=sid)
                        ).pack(side="left", padx=8)
                else:
                    self.app.ttk.Button(
                        actions, text=_("⬇ Download Series"),
                        command=lambda: self.app.open_download_dialog(
                            self.app.current_server, sid, "Series",
                            item.get("Name", "")) ).pack(side="left", padx=8)
                meta = metadata_line(item)
                if meta:
                    self.app.tk.Label(body, text=meta, bg=CARD_BG, fg=SUBTLE_FG,
                                      anchor="w").pack(fill="x", padx=16, pady=(8, 2))
                if item.get("Overview"):
                    self.app.tk.Label(body, text=item["Overview"], bg=CARD_BG,
                                      fg=TEXT_FG, justify="left", anchor="w",
                                      wraplength=820).pack(fill="x", padx=16,
                                                           pady=(0, 6))

            if seasons:
                row = HScrollRow(self.app, body, _("Seasons"),
                                 poster_box(self.app.image_width))
                row.widget().pack(fill="x")
                row.set_items(seasons, server, image_type="Primary",
                              on_click=self._open_season, subtitle_fn=lambda s: "")
            else:
                self.app.tk.Label(body, text=_("No seasons found."), bg=CARD_BG,
                                  fg=SUBTLE_FG).pack(pady=40)

        self.app.run_async(work, done,
                           lambda e: (spinner.destroy(), self._error(self.frame, e)))

    def _open_season(self, season):
        self.app.navigate({
            "kind": "season",
            "series_id": self.route["series_id"],
            "season_id": season["Id"],
            "title": season.get("Name", _("Season")),
            "series_title": self.route.get("title", ""),
        })


class SeasonView(BaseView):
    """Episode grid for one season, with a season switcher and back-to-series."""

    def __init__(self, app, route):
        super().__init__(app, route)
        self.seasons = []
        self.ep_grid = None
        self.watched_btn = None
        self._cur_season_id = route.get("season_id")
        self._season_watched = False

    def _build(self):
        tk, ttk = self.app.tk, self.app.ttk
        server = self.app.current_server
        sid = self.route["series_id"]

        bar = tk.Frame(self.frame, bg=CARD_BG)
        bar.pack(fill="x", padx=8, pady=4)
        ttk.Button(bar, text=_("◀ %s") % self.route.get("series_title", _("Series")),
                   command=self._to_series).pack(side="left")
        ttk.Button(bar, text=_("▶ Play Next Up"), style="Accent.TButton",
                   command=self._play_next_up).pack(side="left", padx=12)
        ttk.Button(bar, text=_("⬇ Download Season"),
                   command=lambda: self.app.open_download_dialog(
                       self.app.current_server, self.route["season_id"], "Season",
                       self.route.get("title", ""))).pack(side="left")
        self.watched_btn = ttk.Button(bar, text=_("Mark watched"),
                                      command=self._toggle_season_watched)
        self.watched_btn.pack(side="left", padx=8)
        tk.Label(bar, text=self.route.get("title", ""), bg=CARD_BG, fg=TEXT_FG,
                 font=("TkDefaultFont", 14, "bold")).pack(side="left", padx=4)

        self.season_var = tk.StringVar()
        self.season_box = ttk.Combobox(bar, textvariable=self.season_var,
                                       state="readonly", width=22)
        self.season_box.pack(side="right")
        self.season_box.bind("<<ComboboxSelected>>", lambda _e: self._on_switch())

        self.ep_grid = ScrollableGrid(self.app, self.frame,
                                      thumb_box(int(self.app.image_width * 1.4)))
        self.ep_grid.widget().pack(fill="both", expand=True)

        spinner = self._spinner(self.frame)

        def work():
            return self.app.source.get_seasons(server, sid)

        def done(seasons):
            spinner.destroy()
            self.seasons = seasons
            names = [s.get("Name", "?") for s in seasons]
            self.season_box.config(values=names)
            cur = next((i for i, s in enumerate(seasons)
                        if s["Id"] == self.route["season_id"]), 0)
            if names:
                self.season_box.current(cur)
            self._load_episodes(self.route["season_id"])

        self.app.run_async(work, done,
                           lambda e: (spinner.destroy(), self._error(self.frame, e)))

    def _to_series(self):
        self.app.navigate({"kind": "series", "series_id": self.route["series_id"],
                           "title": self.route.get("series_title", "")})

    def _play_next_up(self):
        self.app.play_next_up(self.route["series_id"])

    def _on_switch(self):
        idx = self.season_box.current()
        if 0 <= idx < len(self.seasons):
            self._load_episodes(self.seasons[idx]["Id"])

    def _load_episodes(self, season_id):
        server = self.app.current_server
        sid = self.route["series_id"]
        self._cur_season_id = season_id

        def work():
            return self.app.source.get_episodes(server, sid, season_id)

        def done(eps):
            def subtitle(item):
                num = item.get("IndexNumber")
                prefix = ("%d. " % num) if num is not None else ""
                return prefix + item.get("Name", "")

            self.ep_grid.set_items(eps, server, image_type="Thumb",
                                   on_click=self.app.open_item, subtitle_fn=subtitle)
            # Season counts as watched only when every episode is.
            self._season_watched = bool(eps) and all(is_watched(e) for e in eps)
            if self.watched_btn is not None:
                self.watched_btn.config(
                    text=_("Mark unwatched") if self._season_watched
                    else _("Mark watched"))

        self.app.run_async(work, done, lambda e: self._error(self.frame, e))

    def _toggle_season_watched(self):
        if self._cur_season_id:
            self.app.set_watched(self.app.current_server, self._cur_season_id,
                                 not self._season_watched, refresh=True)


class SearchView(BaseView):
    def _build(self):
        server = self.app.current_server
        term = self.route.get("term", "")
        tk = self.app.tk
        tk.Label(self.frame, text=_('Results for "%s"') % term, bg=CARD_BG,
                 fg=TEXT_FG, font=("TkDefaultFont", 14, "bold")).pack(
            anchor="w", padx=8, pady=4)

        grid = ScrollableGrid(self.app, self.frame, poster_box(self.app.image_width))
        grid.widget().pack(fill="both", expand=True)
        spinner = self._spinner(self.frame)

        def work():
            return self.app.source.search(server, term)

        def done(items):
            spinner.destroy()
            if not items:
                tk.Label(self.frame, text=_("No results."), bg=CARD_BG,
                         fg=SUBTLE_FG).pack(pady=40)
                return
            grid.set_items(items, server, image_type="Primary",
                           on_click=self.app.open_item)

        self.app.run_async(work, done,
                           lambda e: (spinner.destroy(), self._error(self.frame, e)))


class DetailView(BaseView):
    """Item detail with backdrop, metadata, resume/play, track pickers."""

    def __init__(self, app, route):
        super().__init__(app, route)
        self.item = None
        self.media_source = None
        self.audio_var = None
        self.sub_var = None
        self._audio_map = {}
        self._sub_map = {}

    def _build(self):
        server = self.app.current_server
        item_id = self.route["item_id"]
        spinner = self._spinner(self.frame)

        def work():
            return self.app.source.get_item(server, item_id)

        def done(item):
            spinner.destroy()
            if not item:
                self._error(self.frame, _("Item not found."))
                return
            self.item = item
            self._render(item)

        self.app.run_async(work, done,
                           lambda e: (spinner.destroy(), self._error(self.frame, e)))

    def _render(self, item):
        tk = self.app.tk

        title = item.get("Name", "")
        if item.get("Type") == "Episode":
            s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
            if s is not None and e is not None:
                title = "%s — S%dE%d · %s" % (
                    item.get("SeriesName", ""), s, e, title)
        build_media_header(self.app, self.frame, item, title_text=title)

        body = tk.Frame(self.frame, bg=CARD_BG)
        body.pack(fill="both", expand=True, padx=16, pady=8)

        meta = metadata_line(item)
        if meta:
            tk.Label(body, text=meta, bg=CARD_BG, fg=SUBTLE_FG, anchor="w").pack(
                fill="x", pady=(0, 6))

        if item.get("Overview"):
            tk.Label(body, text=item["Overview"], bg=CARD_BG, fg=TEXT_FG,
                     justify="left", anchor="w", wraplength=820).pack(
                fill="x", pady=(0, 10))

        self.media_source = self._pick_source(item)
        try:
            self._build_track_pickers(body)
        except Exception:
            # Never let a track-picker failure strand the page without a Play
            # button — fall back to default tracks.
            log.warning("Track picker build failed", exc_info=True)
        self._build_actions(body, item)

    def _pick_source(self, item):
        sources = item.get("MediaSources") or []
        return sources[0] if sources else None

    def _default_track_indices(self):
        """Defaults shown in the pickers, matching what playback will actually do:
        language_config wins, falling back to the server's session default. Keeps
        the UI honest so the user's global preference isn't masked by the server."""
        server_aid = self.media_source.get("DefaultAudioStreamIndex")
        server_sid = self.media_source.get("DefaultSubtitleStreamIndex")
        rules = parse_language_config(self.app.settings_values.get("language_config"))
        rule_aid, rule_sid = apply_language_config(rules, self.media_source, self.item)
        return (rule_aid if rule_aid is not None else server_aid,
                rule_sid if rule_sid is not None else server_sid)

    def _build_track_pickers(self, parent):
        tk, ttk = self.app.tk, self.app.ttk
        if not self.media_source:
            return
        streams = self.media_source.get("MediaStreams") or []
        audios = [s for s in streams if s.get("Type") == "Audio"]
        subs = [s for s in streams if s.get("Type") == "Subtitle"]
        default_aid, default_sid = self._default_track_indices()

        row = tk.Frame(parent, bg=CARD_BG)
        row.pack(fill="x", pady=(4, 8))

        if audios:
            tk.Label(row, text=_("Audio:"), bg=CARD_BG, fg=SUBTLE_FG).pack(side="left")
            self.audio_var = tk.StringVar()
            labels = []
            default_idx = default_aid
            default_label = None
            for s in audios:
                label = s.get("DisplayTitle") or get_sub_display_title(s)
                labels.append(label)
                self._audio_map[label] = s.get("Index")
                if s.get("Index") == default_idx:
                    default_label = label
            box = ttk.Combobox(row, textvariable=self.audio_var, state="readonly",
                               width=30, values=labels)
            box.pack(side="left", padx=(4, 16))
            box.set(default_label or labels[0])

        if subs:
            tk.Label(row, text=_("Subtitles:"), bg=CARD_BG, fg=SUBTLE_FG).pack(
                side="left")
            self.sub_var = tk.StringVar()
            none_label = _("None")
            labels = [none_label]
            self._sub_map[none_label] = None
            default_idx = default_sid
            default_label = none_label
            for s in subs:
                label = s.get("DisplayTitle") or get_sub_display_title(s)
                labels.append(label)
                self._sub_map[label] = s.get("Index")
                if s.get("Index") == default_idx:
                    default_label = label
            box = ttk.Combobox(row, textvariable=self.sub_var, state="readonly",
                               width=30, values=labels)
            box.pack(side="left", padx=4)
            box.set(default_label)

    def _build_actions(self, parent, item):
        ttk = self.app.ttk
        row = self.app.tk.Frame(parent, bg=CARD_BG)
        row.pack(fill="x", pady=8)

        resume_ticks = (item.get("UserData") or {}).get("PlaybackPositionTicks") or 0
        if resume_ticks:
            ttk.Button(row, text=_("Resume from %s") % format_ticks(resume_ticks),
                       style="Accent.TButton",
                       command=lambda: self._play(resume_ticks)).pack(side="left")
            ttk.Button(row, text=_("Play from start"),
                       command=lambda: self._play(0)).pack(side="left", padx=8)
        else:
            ttk.Button(row, text=_("▶ Play"), style="Accent.TButton",
                       command=lambda: self._play(0)).pack(side="left")

        if item.get("Type") == "Episode" and item.get("SeriesId"):
            ttk.Button(row, text=_("Go to Series"),
                       command=self._to_series).pack(side="left", padx=16)

        watched = is_watched(item)
        ttk.Button(row, text=_("Mark unwatched") if watched else _("Mark watched"),
                   command=lambda: self.app.set_watched(
                       self.app.current_server, item["Id"], not watched,
                       refresh=True)).pack(side="left", padx=8)

        if self.app.is_downloaded(item):
            ttk.Button(row, text=_("🗑 Remove download"),
                       command=lambda: self.app.delete_download(item_id=item["Id"])
                       ).pack(side="right")
        else:
            ttk.Button(row, text=_("⬇ Download"),
                       command=lambda: self.app.open_download_dialog(
                           self.app.current_server, item["Id"], item.get("Type"),
                           item.get("Name", ""))).pack(side="right")

    def _to_series(self):
        self.app.navigate({"kind": "series", "series_id": self.item["SeriesId"],
                           "title": self.item.get("SeriesName", "")})

    def _play(self, offset_ticks):
        aid = self._audio_map.get(self.audio_var.get()) if self.audio_var else None
        sid = self._sub_map.get(self.sub_var.get()) if self.sub_var else None
        srcid = self.media_source.get("Id") if self.media_source else None
        if self.item.get("Type") == "Episode":
            # Queue the rest of the season so autoplay-next chains episodes.
            self.app.play_episode(self.item, offset_ticks=offset_ticks,
                                  aid=aid, sid=sid, srcid=srcid)
        else:
            self.app.play({
                "server_uuid": self.app.current_server,
                "item_ids": [self.item["Id"]],
                "start_index": 0,
                "offset_ticks": offset_ticks or None,
                "media_source_id": srcid,
                "audio_index": aid,
                "subtitle_index": sid,
            })


class _ServerForm:
    """Shared server-credential entry form used by Login and Servers views."""

    def __init__(self, app, parent, on_submit, button_text):
        tk, ttk = app.tk, app.ttk
        self.app = app
        self._on_submit = on_submit
        self.server = tk.StringVar()
        self.user = tk.StringVar()
        self.pw = tk.StringVar()

        self.frame = tk.Frame(parent, bg=CARD_BG)
        rows = [(_("Server URL"), self.server, None),
                (_("Username"), self.user, None),
                (_("Password"), self.pw, "*")]
        for label, var, show in rows:
            row = tk.Frame(self.frame, bg=CARD_BG)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=label, bg=CARD_BG, fg=SUBTLE_FG, width=12,
                     anchor="w").pack(side="left")
            entry = ttk.Entry(row, textvariable=var, width=32, show=show or "")
            entry.pack(side="left")
            entry.bind("<Return>", lambda _e: self.submit())

        self.error = tk.Label(self.frame, text="", bg=CARD_BG, fg="#e57373",
                              wraplength=320, justify="center")
        self.error.pack(pady=(8, 0))
        self.button = ttk.Button(self.frame, text=button_text,
                                 style="Accent.TButton", command=self.submit)
        self.button.pack(pady=10)

    def widget(self):
        return self.frame

    def submit(self):
        server = self.server.get().strip()
        user = self.user.get().strip()
        if not server or not user:
            self.error.config(text=_("Server and username are required."))
            return
        self.error.config(text=_("Connecting…"))
        self.button.config(state="disabled")
        self._on_submit({"server": server, "username": user, "password": self.pw.get()})

    def on_result(self, result):
        if result.get("ok"):
            self.error.config(text="")
            self.server.set("")
            self.user.set("")
            self.pw.set("")
            self.button.config(state="normal")
        else:
            self.error.config(
                text=result.get("error") or _("Could not connect. Check your details."))
            self.button.config(state="normal")


class ConnectingView(BaseView):
    """Shown while the main process is connecting, so the window appears
    immediately instead of waiting on the network."""

    def _build(self):
        tk, ttk = self.app.tk, self.app.ttk
        wrap = tk.Frame(self.frame, bg=CARD_BG)
        wrap.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(wrap, text=_("Connecting to your server…"), bg=CARD_BG,
                 fg=TEXT_FG, font=("TkDefaultFont", 14, "bold")).pack(pady=(0, 14))
        self.bar = ttk.Progressbar(wrap, mode="indeterminate", length=260)
        self.bar.pack()
        self.bar.start(12)
        # Escape hatch if there are downloads and connecting drags on.
        if self.app.catalog_path and self.app.sync_items:
            ttk.Button(wrap, text=_("Work offline"),
                       command=lambda: self.app.set_offline(True)).pack(pady=(16, 0))


class LoginView(BaseView):
    """Full-screen first-run / signed-out login with the app logo."""

    def _build(self):
        tk = self.app.tk
        wrap = tk.Frame(self.frame, bg=CARD_BG)
        wrap.place(relx=0.5, rely=0.5, anchor="center")

        try:
            from PIL import Image, ImageTk
            img = Image.open(get_resource("logo_ui.png"))
            img.thumbnail((360, 360), Image.LANCZOS)
            self._logo = ImageTk.PhotoImage(img)
            tk.Label(wrap, image=self._logo, bg=CARD_BG).pack(pady=(0, 14))
        except Exception:
            tk.Label(wrap, text=USER_APP_NAME, bg=CARD_BG, fg=TEXT_FG,
                     font=("TkDefaultFont", 20, "bold")).pack(pady=14)

        # Surface a failed connection to saved accounts, with a retry.
        if self.app._connect_failed and self.app.server_list:
            tk.Label(wrap, text=_("Couldn't reach your saved server(s)."),
                     bg=CARD_BG, fg="#e57373").pack(pady=(0, 6))
            self.app.ttk.Button(wrap, text=_("Retry connection"),
                                command=self.app.retry_connect).pack(pady=(0, 12))

        tk.Label(wrap, text=_("Sign in to your Jellyfin server"), bg=CARD_BG,
                 fg=SUBTLE_FG).pack(pady=(0, 10))
        self.form = _ServerForm(self.app, wrap, self.app.add_server, _("Connect"))
        self.form.widget().pack()

    def on_server_result(self, result):
        # On success the incoming server list re-navigates to Home automatically.
        self.form.on_result(result)


class ServersPanel:
    """Saved-server management, embedded in the Settings notebook."""

    def __init__(self, app, parent):
        self.app = app
        self.container = VScrollFrame(app, parent)
        self.container.widget().pack(fill="both", expand=True)
        self.form = None
        self.refresh()

    def refresh(self):
        tk, ttk = self.app.tk, self.app.ttk
        body = self.container.body()
        for child in body.winfo_children():
            child.destroy()

        tk.Label(body, text=_("Servers"), bg=CARD_BG, fg=TEXT_FG,
                 font=("TkDefaultFont", 16, "bold")).pack(anchor="w", padx=16,
                                                          pady=(12, 8))
        if not self.app.server_list:
            tk.Label(body, text=_("No servers configured yet."), bg=CARD_BG,
                     fg=SUBTLE_FG).pack(anchor="w", padx=16)
        for cred in self.app.server_list:
            row = tk.Frame(body, bg=ENTRY_BG)
            row.pack(fill="x", padx=16, pady=3)
            status = _("Connected") if cred.get("connected") else _("Offline")
            color = "#7bd88f" if cred.get("connected") else "#e57373"
            tk.Label(row, text=cred.get("name", "?"), bg=ENTRY_BG, fg=TEXT_FG,
                     font=("TkDefaultFont", 11, "bold")).pack(side="left", padx=8,
                                                              pady=6)
            tk.Label(row, text=cred.get("username", ""), bg=ENTRY_BG,
                     fg=SUBTLE_FG).pack(side="left", padx=8)
            tk.Label(row, text=status, bg=ENTRY_BG, fg=color).pack(side="left", padx=8)
            ttk.Button(row, text=_("Remove"),
                       command=lambda u=cred.get("uuid"): self.app.remove_server(u)
                       ).pack(side="right", padx=8)

        tk.Label(body, text=_("Add a server"), bg=CARD_BG, fg=TEXT_FG,
                 font=("TkDefaultFont", 13, "bold")).pack(anchor="w", padx=16,
                                                          pady=(18, 4))
        self.form = _ServerForm(self.app, body, self.app.add_server, _("Add Server"))
        self.form.widget().pack(anchor="w", padx=16, pady=(0, 16))

    def on_server_result(self, result):
        if self.form:
            self.form.on_result(result)


class LogsPanel:
    """Live application log viewer, embedded in the Settings notebook."""

    def __init__(self, app, parent):
        self.app = app
        tk, ttk = app.tk, app.ttk
        self.text = tk.Text(parent, bg="#111316", fg="#d8d8d8", wrap="word", bd=0,
                            highlightthickness=0, insertbackground="#d8d8d8",
                            font=("TkFixedFont", 9), state="disabled")
        scroll = ttk.Scrollbar(parent, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self._set("\n".join(app.log_lines))
        app.request_logs()

    def _set(self, content):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", content)
        self.text.config(state="disabled")
        self.text.see("end")

    def on_log_init(self, lines):
        self._set("\n".join(lines))

    def on_log_line(self, line):
        self.text.config(state="normal")
        self.text.insert("end", "\n" + line)
        self.text.config(state="disabled")
        self.text.see("end")


class DownloadsPanel:
    """Offline downloads management: disk usage, status/progress, remove."""

    def __init__(self, app, parent):
        self.app = app
        self.container = VScrollFrame(app, parent)
        self.container.widget().pack(fill="both", expand=True)
        self._rows = {}  # item_id -> status Label
        self.refresh()

    def _open_db(self):
        if not self.app.catalog_path:
            return None
        try:
            return SyncDB(self.app.catalog_path, read_only=True)
        except Exception:
            return None

    def refresh(self):
        tk = self.app.tk
        body = self.container.body()
        for child in body.winfo_children():
            child.destroy()
        self._rows = {}

        db = self._open_db()
        rows = db.list() if db else []
        total = db.total_size() if db else self.app.sync_total
        if db:
            db.close()

        tk.Label(body, text=_("Downloads — %s used") % human_size(total),
                 bg=CARD_BG, fg=TEXT_FG, font=("TkDefaultFont", 16, "bold")).pack(
            anchor="w", padx=16, pady=(12, 8))
        if not rows:
            tk.Label(body, text=_("Nothing downloaded yet."), bg=CARD_BG,
                     fg=SUBTLE_FG).pack(anchor="w", padx=16)
            return

        movies = [r for r in rows if not r.get("series_id")]
        series_order, series_map = [], {}
        for r in rows:
            sid = r.get("series_id")
            if not sid:
                continue
            if sid not in series_map:
                series_map[sid] = []
                series_order.append(sid)
            series_map[sid].append(r)

        if movies:
            tk.Label(body, text=_("Movies & Videos"), bg=CARD_BG, fg=TEXT_FG,
                     font=("TkDefaultFont", 13, "bold")).pack(
                anchor="w", padx=16, pady=(10, 2))
            for row in movies:
                self._item_row(body, row, indent=24)

        for sid in series_order:
            self._series_block(body, sid, series_map[sid])

    @staticmethod
    def _season_title(row):
        try:
            name = json.loads(row.get("item_json") or "{}").get("SeasonName")
        except (TypeError, ValueError):
            name = None
        if name:
            return name
        pidx = row.get("parent_index")
        if pidx == 0:
            return _("Specials")
        if pidx:
            return _("Season %d") % pidx
        return _("Episodes")

    @staticmethod
    def _is_watched(row):
        try:
            return bool(json.loads(row.get("userdata_json") or "{}").get("Played"))
        except ValueError:
            return False

    def _series_block(self, body, series_id, rows):
        tk, ttk = self.app.tk, self.app.ttk
        name = rows[0].get("series_name") or _("Series")
        size = sum(r.get("downloaded_bytes") or r.get("size_bytes") or 0
                   for r in rows)
        header = tk.Frame(body, bg=PANEL_BG)
        header.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(header, text="%s  ·  %d · %s" % (name, len(rows), human_size(size)),
                 bg=PANEL_BG, fg=TEXT_FG, font=("TkDefaultFont", 12, "bold")).pack(
            side="left", padx=8, pady=4)
        ttk.Button(header, text=_("Remove show"),
                   command=lambda s=series_id: self.app.delete_download(series_id=s)
                   ).pack(side="right", padx=4, pady=2)
        if any(self._is_watched(r) for r in rows):
            ttk.Button(header, text=_("Remove watched"),
                       command=lambda s=series_id: self.app.delete_download(
                           series_id=s, watched_only=True)).pack(side="right",
                                                                 padx=4, pady=2)

        # Group episodes by season, preserving aired order.
        season_order, season_map = [], {}
        for r in rows:
            key = r.get("season_id") or ("p%s" % r.get("parent_index"))
            if key not in season_map:
                season_map[key] = []
                season_order.append(key)
            season_map[key].append(r)

        for key in season_order:
            srows = season_map[key]
            season_id = srows[0].get("season_id")
            title = self._season_title(srows[0])
            shdr = tk.Frame(body, bg=CARD_BG)
            shdr.pack(fill="x", padx=(28, 12))
            tk.Label(shdr, text=title, bg=CARD_BG, fg=SUBTLE_FG,
                     font=("TkDefaultFont", 11, "bold")).pack(side="left", pady=2)
            if season_id:
                ttk.Button(shdr, text=_("Remove season"),
                           command=lambda s=series_id, k=season_id:
                           self.app.delete_download(series_id=s, season_id=k)
                           ).pack(side="right", padx=4)
            for row in srows:
                self._item_row(body, row, indent=44, episode=True)

    def _item_row(self, body, row, indent=24, episode=False):
        tk, ttk = self.app.tk, self.app.ttk
        frame = tk.Frame(body, bg=ENTRY_BG)
        frame.pack(fill="x", padx=(indent, 12), pady=1)
        if episode:
            num = row.get("index_number")
            label = ("%d. %s" % (num, row.get("name") or "")) if num is not None \
                else (row.get("name") or row["item_id"])
        else:
            label = row.get("name") or row["item_id"]
        tk.Label(frame, text=label, bg=ENTRY_BG, fg=TEXT_FG, anchor="w").pack(
            side="left", padx=8, pady=5)
        ttk.Button(frame, text=_("Remove"),
                   command=lambda i=row["item_id"]: self.app.delete_download(item_id=i)
                   ).pack(side="right", padx=8)
        lbl = tk.Label(frame, text=self._status_text(row), bg=ENTRY_BG, fg=SUBTLE_FG)
        lbl.pack(side="right", padx=8)
        if self._is_watched(row):
            tk.Label(frame, text=_("watched"), bg=ENTRY_BG, fg="#7bd88f").pack(
                side="right", padx=4)
        self._rows[row["item_id"]] = lbl

    @staticmethod
    def _status_text(row):
        status = row.get("status")
        if status == STATUS_COMPLETE:
            return human_size(row.get("size_bytes") or 0)
        if status == STATUS_DOWNLOADING:
            dl, tot = row.get("downloaded_bytes") or 0, row.get("size_bytes") or 0
            return _("Downloading %d%%") % (int(dl * 100 / tot) if tot else 0)
        if status == STATUS_PENDING:
            return _("Queued")
        if status == STATUS_ERROR:
            return _("Failed")
        return status or ""

    def on_download_progress(self, payload):
        lbl = self._rows.get(payload.get("item_id"))
        if lbl is not None:
            dl, tot = payload.get("downloaded", 0), payload.get("total", 0)
            try:
                lbl.config(text=_("Downloading %d%%") % (
                    int(dl * 100 / tot) if tot else 0))
            except Exception:
                pass

    def on_sync_state(self, _ss):
        self.refresh()


# Friendly, grouped settings (in-player-menu + README-documented keys). Anything
# not listed here shows under the auto-generated "Advanced" toggle.
SETTINGS_SECTIONS = [
    (_("Interface"), ["player_name", "enable_gui", "start_minimized", "fullscreen",
                      "enable_osc", "raise_mpv", "check_updates", "notify_updates"]),
    (_("Playback"), ["audio_output", "auto_play", "always_transcode", "local_kbps",
                     "remote_kbps", "direct_paths", "remote_direct_paths",
                     "playback_timeout"]),
    (_("Subtitles & Languages"), ["subtitle_size", "subtitle_color",
                                  "subtitle_position", "language_preference",
                                  "preferred_language", "remember_audio_track",
                                  "remember_subtitle_track", "lang_filter",
                                  "lang_filter_sub", "lang_filter_audio"]),
    (_("Transcoding"), ["allow_transcode_to_h265", "prefer_transcode_to_h265",
                        "transcode_hevc", "transcode_av1", "transcode_4k",
                        "transcode_hdr", "transcode_hi10p",
                        "transcode_dolby_vision", "force_video_codec",
                        "force_audio_codec"]),
    (_("Skip Intro / Credits"), ["skip_intro_enable", "skip_intro_always",
                                 "skip_credits_enable", "skip_credits_always"]),
    (_("Library Browser"), ["library_page_size", "library_image_width",
                            "library_image_cache_mb"]),
]

SETTINGS_ENUMS = {
    "subtitle_position": ["top", "bottom"],
    "mpv_log_level": ["fatal", "error", "warn", "info", "debug"],
    "shader_pack_subtype": ["lq", "hq"],
}

# Enums with friendly labels distinct from the stored value.
SETTINGS_LABELED_ENUMS = {
    "language_preference": [
        (_("Unset"), "unset"),
        (_("Dubbed (shows only)"), "dubbed_shows"),
        (_("Subbed (shows only)"), "subbed_shows"),
        (_("Dubbed (all)"), "dubbed_all"),
        (_("Subbed (all)"), "subbed_all"),
        (_("Custom (set in config)"), "custom"),
    ],
}

_ACRONYMS = {"gui": "GUI", "ssl": "SSL", "tls": "TLS", "osc": "OSC", "mpv": "MPV",
             "hdr": "HDR", "av1": "AV1", "h265": "H265", "hevc": "HEVC",
             "kbps": "kbps", "url": "URL", "ipc": "IPC", "uuid": "UUID",
             "svp": "SVP", "id": "ID", "4k": "4K", "hi10p": "Hi10P"}


def _label_for(key):
    return " ".join(_ACRONYMS.get(w, w.capitalize()) for w in key.split("_"))


class SettingsPanel:
    """Generated config form: curated sections + an Advanced toggle for the rest."""

    def __init__(self, app, parent):
        self.app = app
        tk = app.tk
        self.vars = {}  # key -> (tk var, type)
        self.show_advanced = tk.BooleanVar(value=False)
        self.container = VScrollFrame(app, parent)
        self.container.widget().pack(fill="both", expand=True)
        self._build()

    def _build(self):
        tk, ttk = self.app.tk, self.app.ttk
        body = self.container.body()
        schema = self.app.settings_schema

        curated = set()
        for title, keys in SETTINGS_SECTIONS:
            present = [k for k in keys if schema.get(k, "skip") != "skip"]
            curated.update(present)
            self._section(body, title, present)

        # Advanced section (built once, shown/hidden by the toggle).
        toggle = ttk.Checkbutton(body, text=_("Show advanced settings"),
                                 variable=self.show_advanced,
                                 command=self._toggle_advanced)
        toggle.pack(anchor="w", padx=16, pady=(14, 0))

        self.adv_frame = tk.Frame(body, bg=CARD_BG)
        advanced = sorted(k for k, t in schema.items()
                          if t != "skip" and k not in curated)
        self._section(self.adv_frame, _("Advanced"), advanced)

        self.save_row = tk.Frame(body, bg=CARD_BG)
        self.save_row.pack(fill="x", padx=16, pady=14)
        self.status = tk.Label(self.save_row, text="", bg=CARD_BG, fg=SUBTLE_FG)
        self.status.pack(side="right", padx=8)
        ttk.Button(self.save_row, text=_("Save Settings"), style="Accent.TButton",
                   command=self._save).pack(side="left")
        tk.Label(body, text=_("Some changes take effect after restarting."),
                 bg=CARD_BG, fg=SUBTLE_FG).pack(anchor="w", padx=16, pady=(0, 16))

    def _toggle_advanced(self):
        if self.show_advanced.get():
            self.adv_frame.pack(fill="x", before=self.save_row)
        else:
            self.adv_frame.pack_forget()

    def _section(self, parent, title, keys):
        tk, ttk = self.app.tk, self.app.ttk
        if not keys:
            return
        tk.Label(parent, text=title, bg=CARD_BG, fg=TEXT_FG,
                 font=("TkDefaultFont", 13, "bold")).pack(anchor="w", padx=16,
                                                          pady=(14, 4))
        grid = tk.Frame(parent, bg=CARD_BG)
        grid.pack(fill="x", padx=16)
        grid.columnconfigure(1, weight=1)
        for row, key in enumerate(keys):
            vtype = self.app.settings_schema.get(key, "str")
            value = self.app.settings_values.get(key)
            tk.Label(grid, text=_label_for(key), bg=CARD_BG, fg=SUBTLE_FG,
                     anchor="w").grid(row=row, column=0, sticky="w", padx=(0, 12),
                                      pady=3)
            if vtype == "bool":
                var = tk.BooleanVar(value=bool(value))
                ttk.Checkbutton(grid, variable=var).grid(row=row, column=1,
                                                         sticky="w", pady=3)
            elif key in SETTINGS_LABELED_ENUMS:
                opts = SETTINGS_LABELED_ENUMS[key]
                val_to_label = {v: l for l, v in opts}
                var = tk.StringVar(value=val_to_label.get(str(value), opts[0][0]))
                ttk.Combobox(grid, textvariable=var, state="readonly", width=28,
                             values=[l for l, _v in opts]).grid(
                    row=row, column=1, sticky="w", pady=3)
                vtype = "labeled"
            elif key in SETTINGS_ENUMS:
                var = tk.StringVar(value="" if value is None else str(value))
                ttk.Combobox(grid, textvariable=var, state="readonly", width=28,
                             values=SETTINGS_ENUMS[key]).grid(row=row, column=1,
                                                              sticky="w", pady=3)
            else:
                var = tk.StringVar(value="" if value is None else str(value))
                ttk.Entry(grid, textvariable=var, width=34).grid(
                    row=row, column=1, sticky="w", pady=3)
            self.vars[key] = (var, vtype)

    def _save(self):
        changes = {}
        for key, (var, vtype) in self.vars.items():
            if vtype == "bool":
                changes[key] = bool(var.get())
            elif vtype == "labeled":
                label_to_val = {l: v for l, v in SETTINGS_LABELED_ENUMS[key]}
                changes[key] = label_to_val.get(var.get(),
                                                SETTINGS_LABELED_ENUMS[key][0][1])
            else:
                changes[key] = var.get().strip()  # main coerces; "" -> None/blank
        self.app.save_settings(changes)
        self.status.config(text=_("Saved."))

    def on_data(self, values):
        # Refresh the displayed values after a save round-trip (coercion may have
        # adjusted them).
        self.app.settings_values = values
        for key, (var, vtype) in self.vars.items():
            if key not in values:
                continue
            if vtype == "bool":
                var.set(values[key])
            elif vtype == "labeled":
                val_to_label = {v: l for l, v in SETTINGS_LABELED_ENUMS[key]}
                var.set(val_to_label.get(str(values[key]),
                                         SETTINGS_LABELED_ENUMS[key][0][0]))
            else:
                var.set("" if values[key] is None else str(values[key]))


class SettingsView(BaseView):
    """Unified Settings screen: Settings / Servers / Logs tabs."""

    def __init__(self, app, route):
        super().__init__(app, route)
        self.settings_panel = None
        self.servers_panel = None
        self.logs_panel = None
        self.downloads_panel = None

    def _build(self):
        tk, ttk = self.app.tk, self.app.ttk
        nb = ttk.Notebook(self.frame)
        nb.pack(fill="both", expand=True)

        settings_tab = tk.Frame(nb, bg=CARD_BG)
        downloads_tab = tk.Frame(nb, bg=CARD_BG)
        servers_tab = tk.Frame(nb, bg=CARD_BG)
        logs_tab = tk.Frame(nb, bg=CARD_BG)
        nb.add(settings_tab, text=_("Settings"))
        nb.add(downloads_tab, text=_("Downloads"))
        nb.add(servers_tab, text=_("Servers"))
        nb.add(logs_tab, text=_("Logs"))

        self.settings_panel = SettingsPanel(self.app, settings_tab)
        self.downloads_panel = DownloadsPanel(self.app, downloads_tab)
        self.servers_panel = ServersPanel(self.app, servers_tab)
        self.logs_panel = LogsPanel(self.app, logs_tab)

        tab = self.route.get("tab")
        if tab == "downloads":
            nb.select(downloads_tab)
        elif tab == "servers":
            nb.select(servers_tab)
        elif tab == "logs":
            nb.select(logs_tab)

    # Forward IPC-driven updates to the relevant panel.
    def on_server_result(self, result):
        if self.servers_panel:
            self.servers_panel.on_server_result(result)

    def on_servers_changed(self, _server_list):
        if self.servers_panel:
            self.servers_panel.refresh()

    def on_log_init(self, lines):
        if self.logs_panel:
            self.logs_panel.on_log_init(lines)

    def on_log_line(self, line):
        if self.logs_panel:
            self.logs_panel.on_log_line(line)

    def on_settings_data(self, values):
        if self.settings_panel:
            self.settings_panel.on_data(values)

    def on_download_progress(self, payload):
        if self.downloads_panel:
            self.downloads_panel.on_download_progress(payload)

    def on_sync_state(self, ss):
        if self.downloads_panel:
            self.downloads_panel.on_sync_state(ss)


class DownloadDialog:
    """Modal: estimate the download, choose whether to include watched, confirm."""

    def __init__(self, app, server_uuid, item_id, item_type, title):
        self.app = app
        self.server_uuid = server_uuid
        self.item_id = item_id
        self.item_type = item_type
        self._is_collection = item_type in ("Series", "Season")
        tk, ttk = app.tk, app.ttk

        win = tk.Toplevel(app.root)
        self.win = win
        win.title(_("Download"))
        win.configure(bg=CARD_BG)
        win.transient(app.root)
        win.geometry("460x250")
        win.resizable(False, False)

        tk.Label(win, text=title or _("Download"), bg=CARD_BG, fg=TEXT_FG,
                 font=("TkDefaultFont", 13, "bold"), wraplength=420,
                 justify="left").pack(anchor="w", padx=16, pady=(14, 4))
        self.info = tk.Label(win, text=_("Estimating download size…"), bg=CARD_BG,
                             fg=SUBTLE_FG, justify="left", wraplength=420)
        self.info.pack(anchor="w", padx=16, pady=4)

        self.include_watched = tk.BooleanVar(value=False)
        if self._is_collection:
            self.watched_chk = ttk.Checkbutton(
                win, text=_("Include watched episodes"),
                variable=self.include_watched, state="disabled")
            self.watched_chk.pack(anchor="w", padx=16, pady=6)

        btns = tk.Frame(win, bg=CARD_BG)
        btns.pack(side="bottom", fill="x", padx=16, pady=14)
        ttk.Button(btns, text=_("Cancel"), command=self.close).pack(side="right")
        self.dl_btn = ttk.Button(btns, text=_("Download"), style="Accent.TButton",
                                 command=self._confirm, state="disabled")
        self.dl_btn.pack(side="right", padx=8)

        win.protocol("WM_DELETE_WINDOW", self.close)
        app.estimate_download(server_uuid, item_id, item_type)

    def on_estimate(self, est):
        count = est.get("count", 0)
        already = est.get("already_count", 0)
        watched = est.get("watched_count", 0)
        lines = [_("%(n)d item(s), about %(size)s") % {
            "n": count, "size": human_size(est.get("total_bytes", 0))}]
        if watched and self._is_collection:
            lines.append(_("%d watched") % watched)
        if already:
            lines.append(_("%d already downloaded") % already)
        self.info.config(text="\n".join(lines))
        if self._is_collection and watched:
            self.watched_chk.config(state="normal")
        self.dl_btn.config(state="normal" if count else "disabled")

    def _confirm(self):
        include = self.include_watched.get() if self._is_collection else True
        self.app.download(self.server_uuid, self.item_id, self.item_type, include)
        self.close()

    def close(self):
        self.app._download_dialog = None
        try:
            self.win.destroy()
        except Exception:
            pass


VIEW_TYPES = {
    "home": HomeView,
    "grid": GridView,
    "series": SeriesView,
    "season": SeasonView,
    "detail": DetailView,
    "search": SearchView,
    "login": LoginView,
    "connecting": ConnectingView,
    "settings": SettingsView,
}
