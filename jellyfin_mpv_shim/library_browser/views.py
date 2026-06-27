"""Screens for the library browser: Home, Grid, Series, Season, Detail, Search.

Each view is built fresh on navigation. Views fetch data off the UI thread via
``app.run_async`` and render on completion, so the window never blocks on the
network.
"""

import logging

from ..i18n import _
from ..constants import USER_APP_NAME
from ..utils import get_sub_display_title, get_resource
from .theme import CARD_BG, TEXT_FG, SUBTLE_FG, WINDOW_BG, ENTRY_BG
from .widgets import (
    ScrollableGrid, HScrollRow, VScrollFrame, format_ticks, make_key,
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
            if not self.rendered:
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

    def _build(self):
        tk, ttk = self.app.tk, self.app.ttk
        server = self.app.current_server
        sid = self.route["series_id"]

        bar = tk.Frame(self.frame, bg=CARD_BG)
        bar.pack(fill="x", padx=8, pady=4)
        ttk.Button(bar, text=_("◀ %s") % self.route.get("series_title", _("Series")),
                   command=self._to_series).pack(side="left")
        tk.Label(bar, text=self.route.get("title", ""), bg=CARD_BG, fg=TEXT_FG,
                 font=("TkDefaultFont", 14, "bold")).pack(side="left", padx=12)

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

    def _on_switch(self):
        idx = self.season_box.current()
        if 0 <= idx < len(self.seasons):
            self._load_episodes(self.seasons[idx]["Id"])

    def _load_episodes(self, season_id):
        server = self.app.current_server
        sid = self.route["series_id"]

        def work():
            return self.app.source.get_episodes(server, sid, season_id)

        def done(eps):
            def subtitle(item):
                num = item.get("IndexNumber")
                prefix = ("%d. " % num) if num is not None else ""
                return prefix + item.get("Name", "")

            self.ep_grid.set_items(eps, server, image_type="Thumb",
                                   on_click=self.app.open_item, subtitle_fn=subtitle)

        self.app.run_async(work, done, lambda e: self._error(self.frame, e))


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
        self._build_track_pickers(body)
        self._build_actions(body, item)

    def _pick_source(self, item):
        sources = item.get("MediaSources") or []
        return sources[0] if sources else None

    def _build_track_pickers(self, parent):
        tk, ttk = self.app.tk, self.app.ttk
        if not self.media_source:
            return
        streams = self.media_source.get("MediaStreams") or []
        audios = [s for s in streams if s.get("Type") == "Audio"]
        subs = [s for s in streams if s.get("Type") == "Subtitle"]

        row = tk.Frame(parent, bg=CARD_BG)
        row.pack(fill="x", pady=(4, 8))

        if audios:
            tk.Label(row, text=_("Audio:"), bg=CARD_BG, fg=SUBTLE_FG).pack(side="left")
            self.audio_var = tk.StringVar()
            labels = []
            default_idx = self.media_source.get("DefaultAudioStreamIndex")
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
            default_idx = self.media_source.get("DefaultSubtitleStreamIndex")
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

        # Placeholder for the future offline-sync feature.
        ttk.Button(row, text=_("Download (coming soon)"), state="disabled").pack(
            side="right")

    def _to_series(self):
        self.app.navigate({"kind": "series", "series_id": self.item["SeriesId"],
                           "title": self.item.get("SeriesName", "")})

    def _play(self, offset_ticks):
        aid = self._audio_map.get(self.audio_var.get()) if self.audio_var else None
        sid = self._sub_map.get(self.sub_var.get()) if self.sub_var else None
        srcid = self.media_source.get("Id") if self.media_source else None
        # v1: queue just this item. A future improvement is to queue the rest of
        # the season for episodes so the player's autoplay-next chains them.
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

        tk.Label(wrap, text=_("Sign in to your Jellyfin server"), bg=CARD_BG,
                 fg=SUBTLE_FG).pack(pady=(0, 10))
        self.form = _ServerForm(self.app, wrap, self.app.add_server, _("Connect"))
        self.form.widget().pack()

    def on_server_result(self, result):
        # On success the incoming server list re-navigates to Home automatically.
        self.form.on_result(result)


class ServersView(BaseView):
    """Manage saved servers (status + remove) and add new ones."""

    def _build(self):
        tk, ttk = self.app.tk, self.app.ttk
        container = VScrollFrame(self.app, self.frame)
        container.widget().pack(fill="both", expand=True)
        body = container.body()

        tk.Label(body, text=_("Servers"), bg=CARD_BG, fg=TEXT_FG,
                 font=("TkDefaultFont", 16, "bold")).pack(anchor="w", padx=16,
                                                          pady=(12, 8))

        servers = self.app.server_list
        if not servers:
            tk.Label(body, text=_("No servers configured yet."), bg=CARD_BG,
                     fg=SUBTLE_FG).pack(anchor="w", padx=16)
        for cred in servers:
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
        self.form.on_result(result)


class LogView(BaseView):
    """Live application log viewer."""

    def _build(self):
        tk, ttk = self.app.tk, self.app.ttk
        bar = tk.Frame(self.frame, bg=CARD_BG)
        bar.pack(fill="x")
        tk.Label(bar, text=_("Application Log"), bg=CARD_BG, fg=TEXT_FG,
                 font=("TkDefaultFont", 14, "bold")).pack(side="left", padx=12, pady=6)

        wrap = tk.Frame(self.frame, bg=CARD_BG)
        wrap.pack(fill="both", expand=True)
        self.text = tk.Text(wrap, bg="#111316", fg="#d8d8d8", wrap="word", bd=0,
                            highlightthickness=0, insertbackground="#d8d8d8",
                            font=("TkFixedFont", 9), state="disabled")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self._set("\n".join(self.app.log_lines))
        self.app.request_logs()

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


VIEW_TYPES = {
    "home": HomeView,
    "grid": GridView,
    "series": SeriesView,
    "season": SeasonView,
    "detail": DetailView,
    "search": SearchView,
    "login": LoginView,
    "servers": ServersView,
    "logs": LogView,
}
