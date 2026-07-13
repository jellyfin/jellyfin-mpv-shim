"""Screens for the library browser: Home, Grid, Series, Season, Detail, Search.

Each view is built fresh on navigation. Views fetch data off the UI thread via
``app.run_async`` and render on completion, so the window never blocks on the
network.
"""

import json
import logging
from datetime import datetime, timedelta

from ..i18n import _
from ..constants import USER_APP_NAME
from ..utils import get_sub_display_title, get_resource
from ..language_config import apply as apply_language_config, parse_language_config
from ..sync.db import (SyncDB, STATUS_COMPLETE, STATUS_DOWNLOADING,
                       STATUS_PENDING, STATUS_ERROR)
from .repository import PLAYLIST_SUPPORTED_TYPES
from .theme import (CARD_BG, TEXT_FG, SUBTLE_FG, WINDOW_BG, ENTRY_BG, PANEL_BG,
                    ACCENT)
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
        # Key on the REAL backdrop owner/tag (not a constant): a constant key
        # served whichever bitmap landed first forever — across offline/online
        # switches and across server-side backdrop changes.
        owner_id, tag = app.source.backdrop_spec(item) or (item.get("Id"), "hdr")
        key = make_key(owner_id, "Backdrop", tag, HEADER_W, HEADER_H)

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
        # Incremented to invalidate in-flight requests within this view (a
        # sort change, a season switch). run_async captures the value at
        # request time and drops results whose token no longer matches.
        self._req_epoch = 0

    def build(self, parent):
        self.frame = self.app.tk.Frame(parent, bg=CARD_BG)
        self._build()
        return self.frame

    def _build(self):
        raise NotImplementedError

    def new_request(self):
        """Bump and return this view's request token. Call when starting work
        that supersedes any earlier in-flight work for the same view."""
        self._req_epoch += 1
        return self._req_epoch

    def run_async(self, work, done, on_error=None, epoch=None):
        """Like app.run_async, but the done/on_error callbacks are dropped if
        this view is no longer the current one (the user navigated away), or
        if `epoch` is given and no longer matches this view's request token
        (a newer request superseded this one). Fixes stale results landing in
        a torn-down or moved-on view."""
        def guard(cb):
            def wrapped(*a, **k):
                if self.app.current_view is not self:
                    return
                if epoch is not None and epoch != self._req_epoch:
                    return
                return cb(*a, **k)
            return wrapped

        self.app.run_async(
            work, guard(done), guard(on_error) if on_error else None
        )

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
        # Capture a request token so a later on_sync_state reload (which bumps
        # it) supersedes this initial fetch instead of racing it — otherwise a
        # slow initial result could land over fresher offline data.
        epoch = self.new_request()

        def work():
            libraries = self.app.source.get_libraries(server)
            return (libraries,
                    self.app.source.get_home_rows(server, libraries=libraries))

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

        self.run_async(work, done, fail, epoch=epoch)

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
            has_episode = any(i.get("Type") == "Episode" for i in row["items"])
            # Per-library rows carry a CollectionType and are classified by it,
            # not by item types: a TV "Latest" row mixes grouped Series with
            # stray recently-added Episodes, so a type scan would flip the whole
            # row landscape on one episode. Rows without a CollectionType
            # (Continue Watching, Next Up) keep the item-type heuristic.
            ctype = row.get("collection_type")
            if ctype in ("movies", "tvshows", "boxsets"):
                box = poster_box(self.app.image_width)
                image_type = "Primary"
            elif ctype:
                # Home-video / music-video / misc libraries: landscape
                # frame-grabs with no poster. Episodes carry a dedicated Thumb;
                # other items only have a (landscape) Primary.
                box = thumb_box(int(self.app.image_width * 1.4))
                image_type = "Thumb" if has_episode else "Primary"
            elif has_episode:
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

    def on_sync_state(self, _ss):
        # While browsing offline, a download that finishes should appear on the
        # home screen without the user re-entering offline mode. Reload the
        # catalog snapshot off the Tk thread and re-render only if it changed.
        # (Online, live data already refreshes on navigation; nothing to do.)
        if not self.app.is_offline:
            return
        source = self.app.source
        reload_fn = getattr(source, "reload", None)
        if reload_fn is None:
            return
        server = self.server
        epoch = self.new_request()

        def work():
            try:
                reload_fn()
            except Exception:
                log.debug("Offline source reload failed", exc_info=True)
            return (source.get_libraries(server), source.get_home_rows(server))

        def done(result):
            self.app.home_cache[server] = result
            new_sig = self._signature(result)
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

        self.run_async(work, done, lambda _e: None, epoch=epoch)


SORTS = [
    (_("Name"), "SortName", "Ascending"),
    (_("Date Added"), "DateCreated", "Descending"),
    (_("Release Date"), "PremiereDate", "Descending"),
    (_("Community Rating"), "CommunityRating", "Descending"),
    (_("Critic Rating"), "CriticRating", "Descending"),
    (_("Date Played"), "DatePlayed", "Descending"),
    (_("Play Count"), "PlayCount", "Descending"),
    (_("Runtime"), "Runtime", "Ascending"),
    (_("Parental Rating"), "OfficialRating", "Ascending"),
    (_("Random"), "Random", "Ascending"),
]

# The A–Z jump strip: '#' = names not starting with a letter.
ALPHA_LETTERS = ["#"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]


class GridView(BaseView):
    """Infinite-scrolling grid of the children of a library/folder, or a
    person's filmography (route key ``person_id`` instead of ``parent_id``).
    Library grids get a filter bar (unplayed/favorites/genre), an A–Z jump
    strip, and shuffle play."""

    def __init__(self, app, route):
        super().__init__(app, route)
        self.sort_idx = route.get("sort_idx", 0)
        self.grid = None
        self.status = None
        self.total = None
        self.loaded = 0
        self.loading = False
        self._first = True
        self.filters = {"unplayed": False, "favorite": False,
                        "genre": None, "year": None, "letter": None}
        self._letter_labels = {}
        self._genre_box = None
        # When on, the grid lists this Movie library's Collections (BoxSets)
        # instead of its movies.
        self._collections_mode = False

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

        if self.route.get("parent_id"):
            self._build_filter_bar()

        self.grid = ScrollableGrid(self.app, self.frame,
                                   poster_box(self.app.image_width))
        self.grid.widget().pack(fill="both", expand=True)
        self.grid.on_near_end = self._load_more

        self.status = tk.Label(self.frame, text="", bg=CARD_BG, fg=SUBTLE_FG,
                               anchor="w")
        self.status.pack(fill="x", padx=8, pady=2)
        self.status.bind("<Button-1>", self._on_status_click)
        self._retry_armed = False

        self._reset_and_load()

    def _on_status_click(self, _e):
        if not self._retry_armed:
            return
        self._retry_armed = False
        self.status.config(cursor="")
        self._load_more()

    def _build_filter_bar(self):
        tk, ttk = self.app.tk, self.app.ttk
        fbar = tk.Frame(self.frame, bg=CARD_BG)
        fbar.pack(fill="x", padx=8, pady=(0, 2))
        self.unplayed_var = tk.BooleanVar(value=False)
        self.favorite_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(fbar, text=_("Unplayed"), variable=self.unplayed_var,
                        command=self._on_filter_change).pack(side="left")
        ttk.Checkbutton(fbar, text=_("Favorites"), variable=self.favorite_var,
                        command=self._on_filter_change).pack(side="left",
                                                             padx=(10, 0))
        # Collections (BoxSets) are movie-specific and, like jellyfin-web, not a
        # browse tile — offer them here as a toggle on Movie libraries only.
        # Offline has no collections, so it's online-only.
        self.collections_var = tk.BooleanVar(value=False)
        if (self.route.get("collection_type") == "movies"
                and not self.app.is_offline):
            ttk.Checkbutton(fbar, text=_("Collections"),
                            variable=self.collections_var,
                            command=self._on_collections_toggle).pack(
                                side="left", padx=(10, 0))
        tk.Label(fbar, text=_("Genre:"), bg=CARD_BG, fg=SUBTLE_FG).pack(
            side="left", padx=(16, 4))
        self._all_genres_label = _("All genres")
        self.genre_var = tk.StringVar(value=self._all_genres_label)
        self._genre_box = ttk.Combobox(fbar, textvariable=self.genre_var,
                                       state="readonly", width=18,
                                       values=[self._all_genres_label])
        self._genre_box.pack(side="left")
        self._genre_box.bind("<<ComboboxSelected>>",
                             lambda _e: self._on_filter_change())
        tk.Label(fbar, text=_("Year:"), bg=CARD_BG, fg=SUBTLE_FG).pack(
            side="left", padx=(12, 4))
        self._all_years_label = _("All years")
        self.year_var = tk.StringVar(value=self._all_years_label)
        self._year_box = ttk.Combobox(fbar, textvariable=self.year_var,
                                      state="readonly", width=10,
                                      values=[self._all_years_label])
        self._year_box.pack(side="left")
        self._year_box.bind("<<ComboboxSelected>>",
                            lambda _e: self._on_filter_change())
        ttk.Button(fbar, text=_("🔀 Shuffle"),
                   command=self._shuffle).pack(side="right")

        abar = tk.Frame(self.frame, bg=CARD_BG)
        abar.pack(fill="x", padx=8, pady=(0, 2))
        for letter in ALPHA_LETTERS:
            lbl = tk.Label(abar, text=letter, bg=CARD_BG, fg=SUBTLE_FG,
                           font=("TkDefaultFont", 8), cursor="hand2")
            lbl.pack(side="left", padx=2)
            lbl.bind("<Button-1>", lambda _e, l=letter: self._on_letter(l))
            self._letter_labels[letter] = lbl

        # Fill the genre/year pickers in the background; the grid doesn't
        # wait on them.
        server = self.app.current_server
        parent = self.route.get("parent_id")

        def work():
            return self.app.source.get_filter_values(server, parent)

        def done(values):
            genres = values.get("genres") or []
            if genres:
                self._genre_box.config(
                    values=[self._all_genres_label] + list(genres))
            years = values.get("years") or []
            if years:
                self._year_box.config(
                    values=[self._all_years_label] + [str(y) for y in years])

        self.run_async(work, done, lambda _e: None)

    def _on_filter_change(self):
        genre = self.genre_var.get()
        year = self.year_var.get()
        self.filters["unplayed"] = bool(self.unplayed_var.get())
        self.filters["favorite"] = bool(self.favorite_var.get())
        self.filters["genre"] = (None if genre == self._all_genres_label
                                 else genre)
        try:
            self.filters["year"] = (None if year == self._all_years_label
                                    else int(year))
        except ValueError:
            self.filters["year"] = None
        self._reset_and_load()

    def _on_collections_toggle(self):
        self._collections_mode = bool(self.collections_var.get())
        self._reset_and_load()

    def _on_letter(self, letter):
        # Clicking the active letter clears the jump filter.
        self.filters["letter"] = (None if self.filters.get("letter") == letter
                                  else letter)
        active = self.filters["letter"]
        for l, lbl in self._letter_labels.items():
            lbl.config(fg=ACCENT if l == active else SUBTLE_FG,
                       font=("TkDefaultFont", 8,
                             "bold" if l == active else "normal"))
        self._reset_and_load()

    def _shuffle(self):
        server = self.app.current_server
        parent = self.route.get("parent_id")

        def work():
            return self.app.source.get_shuffle_ids(server, parent)

        def done(ids):
            if ids:
                self.app.play({"server_uuid": server, "item_ids": ids,
                               "start_index": 0})

        self.run_async(work, done, lambda _e: None)

    def _on_sort(self, _e):
        self.sort_idx = [s[0] for s in SORTS].index(self.sort_var.get())
        self._reset_and_load()

    def tile_context_actions(self, item):
        # Inside a collection (BoxSet) grid, tiles offer removal in place.
        if (self.route.get("parent_type") == "BoxSet"
                and not self.app.is_offline
                and getattr(self.app, "edit_apis", False)):
            return [(_("Remove from collection"),
                     lambda: self._remove_from_collection(item))]
        return []

    def _remove_from_collection(self, item):
        self.app.collection_edit({
            "op": "remove", "server_uuid": self.app.current_server,
            "collection_id": self.route["parent_id"],
            "item_ids": [item["Id"]]})

    def on_edit_result(self, result):
        if (result or {}).get("kind") == "collection" and result.get("ok"):
            self._reset_and_load()

    def _reset_and_load(self):
        # Supersede any page fetch still in flight from the previous sort, so
        # its result can't append to the freshly reset grid.
        self.new_request()
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
        epoch = self._req_epoch
        person = self.route.get("person_id")
        collections = self._collections_mode
        filters = dict(self.filters)  # snapshot: the UI can change mid-fetch

        def work():
            if collections:
                return self.app.source.get_movie_collections(
                    server, sort_by=sort_by, sort_order=order,
                    start_index=start, limit=self.app.page_size,
                    filters=filters)
            if person:
                return self.app.source.get_person_items(
                    server, person, start_index=start,
                    limit=self.app.page_size, sort_by=sort_by, sort_order=order)
            return self.app.source.get_library_items(
                server, self.route["parent_id"], sort_by=sort_by,
                sort_order=order, start_index=start, limit=self.app.page_size,
                filters=filters)

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
            self._retry_armed = False
            if total:
                self.status.config(text=_("%d of %d") % (self.loaded, total),
                                   cursor="")
            else:
                self.status.config(text=_("Nothing here."), cursor="")

        def fail(e):
            self.loading = False
            log.warning("Grid load failed: %s", e)
            # Re-arm infinite scroll so a mid-list failure can retry on
            # scroll... but with zero tiles the near-end trigger can never
            # fire (nothing is scrollable), so also make the status line an
            # explicit retry affordance.
            self.grid.rearm_near_end()
            self._retry_armed = True
            self.status.config(text=_("Failed to load — click here to retry."),
                               cursor="hand2")

        self.run_async(work, done, fail, epoch=epoch)


class _DetailRowsMixin:
    """Shared 'Cast & Crew' and 'More Like This' rows for the detail-style
    pages (movies/episodes in DetailView, shows in SeriesView). Both take a
    fetched ``item`` (People come from DETAIL_FIELDS) and a scrollable
    ``parent``; the similar row loads after render so it never blocks the page.
    """

    def _build_people_row(self, parent, item):
        people = (item.get("People") or [])[:24]
        if not people:
            return
        row = HScrollRow(self.app, parent, _("Cast & Crew"),
                         poster_box(int(self.app.image_width * 0.7)))
        row.widget().pack(fill="x")
        tiles = []
        for p in people:
            entry = dict(p)
            # People entries use Type for the job (Actor/Director); retag so
            # tiles don't treat them as playable/watchable items.
            entry["_role"] = p.get("Role") or p.get("Type") or ""
            entry["Type"] = "Person"
            tiles.append(entry)
        row.set_items(tiles, self.app.current_server, image_type="Primary",
                      on_click=self._open_person,
                      subtitle_fn=lambda p: p.get("_role", ""))

    def _open_person(self, person):
        if self.app.is_offline:
            return  # people aren't cached offline
        self.app.navigate({"kind": "grid", "person_id": person["Id"],
                           "title": person.get("Name", "")})

    def _load_similar_row(self, parent, item):
        """"More Like This", fetched after the page renders so it never holds
        the detail view hostage. Empty (older apiclient, offline) = no row."""
        server = self.app.current_server

        def work():
            return self.app.source.get_similar(server, item["Id"])

        def done(items):
            if not items:
                return
            row = HScrollRow(self.app, parent, _("More Like This"),
                             poster_box(self.app.image_width))
            row.widget().pack(fill="x")
            row.set_items(items, server, image_type="Primary",
                          on_click=self.app.open_item)

        self.run_async(work, done, lambda _e: None)


class SeriesView(_DetailRowsMixin, BaseView):
    """Series overview: backdrop, metadata, overview, and a row of seasons."""

    def __init__(self, app, route):
        super().__init__(app, route)
        self._item = None
        self._actions = None
        self._download_btn = None

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
                self.app.ttk.Button(
                    actions, text=_("🔀 Shuffle"),
                    command=lambda: self._shuffle(sid)).pack(side="left",
                                                             padx=8)
                wlabel = (_("Mark unwatched") if is_watched(item)
                          else _("Mark watched"))
                self.app.ttk.Button(
                    actions, text=wlabel,
                    command=lambda: self.app.set_watched(
                        server, sid, not is_watched(item), refresh=True)
                    ).pack(side="left", padx=8)
                self._fav_state = bool(
                    (item.get("UserData") or {}).get("IsFavorite"))
                self._fav_btn = self.app.ttk.Button(
                    actions, text=self._fav_label(),
                    command=lambda: self._toggle_favorite(sid))
                self._fav_btn.pack(side="left")
                self._item = item
                self._actions = actions
                self._download_btn = self._make_download_button(actions, sid, item)
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

            # Cast & Crew and More Like This, same as the movie/episode page.
            if item:
                self._build_people_row(body, item)
                self._load_similar_row(body, item)

        self.run_async(work, done,
                           lambda e: (spinner.destroy(), self._error(self.frame, e)))

    def _shuffle(self, sid):
        server = self.app.current_server

        def work():
            import random
            eps = self.app.source.get_series_queue(server, sid, limit=200)
            ids = [e.get("Id") for e in eps if e.get("Id")]
            random.shuffle(ids)
            return ids

        def done(ids):
            if ids:
                self.app.play({"server_uuid": server, "item_ids": ids,
                               "start_index": 0})

        self.run_async(work, done, lambda _e: None)

    def _fav_label(self):
        return (_("♥ Unfavorite") if getattr(self, "_fav_state", False)
                else _("♡ Favorite"))

    def _toggle_favorite(self, sid):
        self._fav_state = not self._fav_state
        self.app.set_favorite(self.app.current_server, sid, self._fav_state)
        if self._item is not None:
            self._item.setdefault("UserData", {})["IsFavorite"] = self._fav_state
        self._fav_btn.config(text=self._fav_label())

    def _make_download_button(self, actions, sid, item):
        ttk = self.app.ttk
        if sid in self.app.sync_series:
            btn = ttk.Button(actions, text=_("🗑 Remove downloads"),
                             command=lambda: self.app.delete_download(series_id=sid))
        else:
            btn = ttk.Button(actions, text=_("⬇ Download Series"),
                             command=lambda: self.app.open_download_dialog(
                                 self.app.current_server, sid, "Series",
                                 item.get("Name", "")))
        btn.pack(side="left", padx=8)
        return btn

    def on_sync_state(self, _ss):
        # Swap only the download button in place instead of rebuilding the whole
        # series page (which would reset scroll on every queue change).
        if self._item is None or self._actions is None:
            return
        try:
            self._download_btn.destroy()
        except Exception:
            pass
        self._download_btn = self._make_download_button(
            self._actions, self.route["series_id"], self._item)

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

        self.run_async(work, done,
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
        # Supersede a previous season's fetch so a slow response can't land
        # its episodes (and its watched state) over the season now selected.
        epoch = self.new_request()

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

        self.run_async(work, done, lambda e: self._error(self.frame, e),
                       epoch=epoch)

    def _toggle_season_watched(self):
        if self._cur_season_id:
            self.app.set_watched(self.app.current_server, self._cur_season_id,
                                 not self._season_watched, refresh=True)


class PlaylistView(BaseView):
    """A playlist's items in playlist order: play-from-here, Play All, and a
    bulk Download Playlist action for offline trips."""

    def __init__(self, app, route):
        super().__init__(app, route)
        self.items = []
        self.grid = None

    def _build(self):
        tk, ttk = self.app.tk, self.app.ttk
        server = self.app.current_server
        pid = self.route["playlist_id"]

        # Title shows immediately; the Play/Download actions are added in done()
        # only once we know the playlist has supported media to act on.
        self.bar = tk.Frame(self.frame, bg=CARD_BG)
        self.bar.pack(fill="x", padx=8, pady=4)
        self._title = tk.Label(self.bar, text=self.route.get("title", ""),
                               bg=CARD_BG, fg=TEXT_FG,
                               font=("TkDefaultFont", 14, "bold"))
        self._title.pack(side="left", padx=4)

        spinner = self._spinner(self.frame)

        def work():
            return self.app.source.get_playlist_items(server, pid)

        def done(raw_items):
            spinner.destroy()
            # A playlist can mix in music/other entries (Jellyfin doesn't
            # constrain contents to the playlist's declared type). Show only
            # supported media, and distinguish an empty playlist from one that
            # holds only unsupported entries.
            items = [it for it in raw_items
                     if it.get("Type") in PLAYLIST_SUPPORTED_TYPES]
            self.items = items
            if not raw_items:
                tk.Label(self.frame, text=_("This playlist is empty."),
                         bg=CARD_BG, fg=SUBTLE_FG).pack(pady=40)
                return
            if not items:
                tk.Label(self.frame,
                         text=_("Playlist does not contain any supported "
                                "media types."),
                         bg=CARD_BG, fg=SUBTLE_FG, wraplength=420).pack(pady=40)
                # The editor operates on every entry regardless of type, so
                # still offer it here — it's the only way to clean stray
                # unsupported entries out of the playlist.
                self._add_edit_button(pid)
                return

            # Now that there's playable content, offer Play All plus a
            # download/delete action, packed left of the title (like the season
            # view). Offline you can't download — offer to remove the downloads.
            ttk.Button(self.bar, text=_("▶ Play All"), style="Accent.TButton",
                       command=lambda: self._play_from(0)).pack(
                           side="left", before=self._title)
            ttk.Button(self.bar, text=_("🔀 Shuffle"),
                       command=self._shuffle).pack(
                           side="left", padx=(4, 0), before=self._title)
            if self.app.is_offline:
                ttk.Button(self.bar, text=_("🗑 Delete Downloads"),
                           command=self._delete_downloads).pack(
                               side="left", padx=12, before=self._title)
            else:
                ttk.Button(self.bar, text=_("⬇ Download Playlist"),
                           command=lambda: self.app.open_download_dialog(
                               server, pid, "Playlist",
                               self.route.get("title", ""))).pack(
                                   side="left", padx=12, before=self._title)
            self._add_edit_button(pid)

            # Episode-heavy playlists read better as landscape stills; movie
            # playlists as posters (mirrors the home rows' heuristic).
            if any(it.get("Type") == "Episode" for it in items):
                box, image_type = thumb_box(int(self.app.image_width * 1.4)), "Thumb"
            else:
                box, image_type = poster_box(self.app.image_width), "Primary"

            def subtitle(item):
                pos = self._index_of(item) + 1
                name = item.get("Name", "")
                series = item.get("SeriesName")
                if series:
                    return "%d. %s – %s" % (pos, series, name)
                return "%d. %s" % (pos, name)

            self.grid = ScrollableGrid(self.app, self.frame, box)
            self.grid.widget().pack(fill="both", expand=True)
            self.grid.set_items(items, server, image_type=image_type,
                                on_click=self._on_click, subtitle_fn=subtitle)

        self.run_async(work, done,
                       lambda e: (spinner.destroy(), self._error(self.frame, e)))

    def _index_of(self, item):
        iid = item.get("Id")
        return next((i for i, it in enumerate(self.items)
                     if it.get("Id") == iid), 0)

    def tile_context_actions(self, item):
        # Quick single-entry removal without entering the editor.
        if self.app.is_offline or not getattr(self.app, "edit_apis", False):
            return []
        entry_id = item.get("PlaylistItemId")
        if not entry_id:
            return []
        return [(_("Remove from playlist"),
                 lambda: self.app.playlist_edit({
                     "op": "remove",
                     "server_uuid": self.app.current_server,
                     "playlist_id": self.route["playlist_id"],
                     "entry_ids": [entry_id]}))]

    def on_edit_result(self, result):
        if (result or {}).get("kind") == "playlist" and result.get("ok"):
            self.app._render_top()  # re-fetch the playlist contents

    def _delete_downloads(self):
        # Removes only the items this playlist pulled down (owned); anything
        # downloaded another way is left alone. Then step back to the list.
        self.app.delete_download(playlist_id=self.route["playlist_id"])
        self.app.go_back()

    def _on_click(self, item):
        self._play_from(self._index_of(item))

    def _add_edit_button(self, pid):
        """Add the ✏ Edit action to the bar, when the server exposes the
        playlist-edit APIs and we're online. Editing works on all entries
        regardless of type, so it's offered even when nothing is playable."""
        if self.app.is_offline or not getattr(self.app, "edit_apis", False):
            return
        self.app.ttk.Button(
            self.bar, text=_("✏ Edit"),
            command=lambda: self.app.navigate(
                {"kind": "playlist_edit", "playlist_id": pid,
                 "title": self.route.get("title", "")})).pack(
                     side="right", padx=8)

    def _shuffle(self):
        """Play the playlist's supported items in a random order. The items are
        already in memory, so shuffle a copy of the id list locally rather than
        asking the server."""
        import random
        ids = [it.get("Id") for it in self.items if it.get("Id")]
        if not ids:
            return
        random.shuffle(ids)
        self.app.play({
            "server_uuid": self.app.current_server,
            "item_ids": ids,
            "start_index": 0,
            "offset_ticks": None,
            "media_source_id": None,
            "audio_index": None,
            "subtitle_index": None,
        })

    def _play_from(self, start_index):
        """Play the whole playlist as a queue, starting at ``start_index`` so
        the player advances through the playlist (not, e.g., the series an
        episode belongs to)."""
        ids = [it.get("Id") for it in self.items if it.get("Id")]
        if not ids:
            return
        start_index = max(0, min(start_index, len(self.items) - 1))
        start_item = self.items[start_index]
        # Re-derive the start position within the filtered id list so an item
        # missing an Id can't shift the queue out from under the chosen entry.
        try:
            pos = ids.index(start_item.get("Id"))
        except ValueError:
            pos = 0
        offset = (start_item.get("UserData") or {}).get(
            "PlaybackPositionTicks") or None
        self.app.play({
            "server_uuid": self.app.current_server,
            "item_ids": ids,
            "start_index": pos,
            "offset_ticks": offset,
            "media_source_id": None,
            "audio_index": None,
            "subtitle_index": None,
        })


class PlaylistEditView(BaseView):
    """Bulk playlist editor. jellyfin-web only offers one-at-a-time
    right-click removal (removing an accidentally-added show means 48
    separate clicks); here the entries live in a multi-select list
    (shift/ctrl-click) with block move and bulk remove.

    The local list is the working model: operations update it immediately
    and stream to the server as one message each (one DELETE for any number
    of entries; moves as an ordered batch whose sequential application
    reproduces the local order). A failed change reloads from the server."""

    def __init__(self, app, route):
        super().__init__(app, route)
        self.entries = []   # raw playlist entries, ALL types, server order
        self.tree = None
        self._title_lbl = None
        self._public_var = None
        self._public_chk = None
        # Guards the visibility checkbox's command against firing when we set
        # the box programmatically to reflect the server's current value.
        self._suppress_vis = False

    def _build(self):
        tk, ttk = self.app.tk, self.app.ttk

        bar = tk.Frame(self.frame, bg=CARD_BG)
        bar.pack(fill="x", padx=8, pady=4)
        self._title_lbl = tk.Label(
            bar, text=_("Edit: %s") % self.route.get("title", ""),
            bg=CARD_BG, fg=TEXT_FG, font=("TkDefaultFont", 14, "bold"))
        self._title_lbl.pack(side="left", padx=4)
        ttk.Button(bar, text=_("Done"), style="Accent.TButton",
                   command=self.app.go_back).pack(side="right")
        ttk.Button(bar, text=_("✎ Rename"),
                   command=self._rename).pack(side="right", padx=(0, 6))
        # Visibility mirrors the playlist's OpenAccess. Start disabled and only
        # enable once the real value has loaded, so a toggle can never send a
        # change derived from the default rather than the server's state.
        self._public_var = tk.BooleanVar(value=False)
        self._public_chk = ttk.Checkbutton(
            bar, text=_("Public (all users)"), variable=self._public_var,
            command=self._toggle_public)
        self._public_chk.state(["disabled"])
        self._public_chk.pack(side="right", padx=(0, 12))
        ttk.Button(bar, text=_("🗑 Delete playlist"),
                   command=self._delete).pack(side="right", padx=(0, 12))

        tools = tk.Frame(self.frame, bg=CARD_BG)
        tools.pack(fill="x", padx=8, pady=(0, 4))
        for label, cmd in ((_("⏫ Top"), self._move_top),
                           (_("🔼 Up"), self._move_up),
                           (_("🔽 Down"), self._move_down),
                           (_("⏬ Bottom"), self._move_bottom)):
            ttk.Button(tools, text=label, command=cmd).pack(side="left",
                                                            padx=(0, 4))
        ttk.Button(tools, text=_("🗑 Remove selected"),
                   command=self._remove_selected).pack(side="left", padx=12)
        tk.Label(tools, text=_("Shift/Ctrl-click to select multiple"),
                 bg=CARD_BG, fg=SUBTLE_FG).pack(side="right")

        holder = tk.Frame(self.frame, bg=CARD_BG)
        holder.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tree = ttk.Treeview(holder, columns=("num", "title", "type",
                                                  "runtime"),
                                 show="headings", selectmode="extended")
        self.tree.heading("num", text="#")
        self.tree.heading("title", text=_("Title"))
        self.tree.heading("type", text=_("Type"))
        self.tree.heading("runtime", text=_("Runtime"))
        self.tree.column("num", width=50, stretch=False, anchor="e")
        self.tree.column("title", width=520)
        self.tree.column("type", width=90, stretch=False)
        self.tree.column("runtime", width=80, stretch=False, anchor="e")
        scroll = ttk.Scrollbar(holder, orient="vertical",
                               command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        # Route the app-wide wheel handler into the list.
        self.tree._wheel_scroll = lambda units: self.tree.yview_scroll(
            units, "units")

        self._load()
        self._load_visibility()

    def _load(self):
        server = self.app.current_server
        pid = self.route["playlist_id"]
        epoch = self.new_request()

        def work():
            return self.app.source.get_playlist_items(server, pid)

        def done(raw):
            # Every entry is editable — including unsupported (e.g. music)
            # ones, so stray entries can be cleaned out and move indices
            # always match the server's view of the playlist.
            self.entries = list(raw)
            self._rebuild()

        self.run_async(work, done, lambda _e: None, epoch=epoch)

    def _load_visibility(self):
        """Fetch the playlist's current OpenAccess and reflect it in the
        Public checkbox, enabling the box once we have a real value. Servers
        (or apiclients) too old to report visibility leave the box disabled."""
        server = self.app.current_server
        pid = self.route["playlist_id"]

        def work():
            return self.app.source.get_playlist(server, pid)

        def done(meta):
            if self._public_chk is None:
                return
            try:
                if not meta or "OpenAccess" not in meta:
                    return  # can't read it → can't safely set it; stay disabled
                self._suppress_vis = True
                self._public_var.set(bool(meta["OpenAccess"]))
                self._suppress_vis = False
                self._public_chk.state(["!disabled"])
            except Exception:
                pass  # view torn down mid-load

        self.run_async(work, done, lambda _e: None)

    def _rename(self):
        from tkinter import simpledialog
        current = self.route.get("title", "")
        name = simpledialog.askstring(
            _("Rename Playlist"), _("New name:"), parent=self.app.root,
            initialvalue=current)
        if name is None:
            return
        name = name.strip()
        if not name or name == current:
            return
        # Optimistic: update the local title now, stream the rename, and let a
        # failed edit_result surface the error (the title reverts on reload).
        self.route["title"] = name
        if self._title_lbl is not None:
            self._title_lbl.config(text=_("Edit: %s") % name)
        self._send({"op": "update", "name": name})

    def _toggle_public(self):
        # Fired only by user interaction; skip the programmatic sync in _load.
        if self._suppress_vis:
            return
        self._send({"op": "update",
                    "is_public": bool(self._public_var.get())})

    def _delete(self):
        from tkinter import messagebox
        title = self.route.get("title", "")
        if not messagebox.askyesno(
                _("Delete Playlist"),
                _('Delete the playlist "%s"? It is removed for all users; the '
                  'videos in it are not deleted.') % title,
                parent=self.app.root, icon="warning", default="no"):
            return
        # On success on_edit_result unwinds to the playlist list; on failure it
        # reloads (the playlist is still there).
        self._send({"op": "delete"})

    @staticmethod
    def _entry_title(item):
        name = item.get("Name", "")
        series = item.get("SeriesName")
        if series:
            s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
            if s is not None and e is not None:
                return "%s — S%02dE%02d · %s" % (series, s, e, name)
            return "%s — %s" % (series, name)
        return name

    def _rebuild(self):
        tree = self.tree
        try:
            keep = set(tree.selection())
            offset = tree.yview()[0]
            tree.delete(*tree.get_children())
        except Exception:
            return  # view torn down
        for i, entry in enumerate(self.entries):
            tree.insert("", "end", iid=entry.get("PlaylistItemId") or str(i),
                        values=(i + 1, self._entry_title(entry),
                                entry.get("Type", ""),
                                format_ticks(entry.get("RunTimeTicks"))))
        still = [iid for iid in keep if tree.exists(iid)]
        if still:
            tree.selection_set(still)
        tree.yview_moveto(offset)

    def _selected_positions(self):
        sel = set(self.tree.selection())
        return [i for i, e in enumerate(self.entries)
                if (e.get("PlaylistItemId") or str(i)) in sel]

    def _send(self, payload):
        payload.update({"server_uuid": self.app.current_server,
                        "playlist_id": self.route["playlist_id"]})
        self.app.playlist_edit(payload)

    def _apply_moves(self, moves):
        self._rebuild()
        if moves:
            self._send({"op": "move", "moves": moves})

    def _move_up(self):
        moves, floor = [], -1
        for idx in self._selected_positions():
            if idx - 1 > floor:
                entry = self.entries.pop(idx)
                self.entries.insert(idx - 1, entry)
                moves.append((entry["PlaylistItemId"], idx - 1))
                floor = idx - 1
            else:
                floor = idx  # block already packed against the top
        self._apply_moves(moves)

    def _move_down(self):
        moves, ceil = [], len(self.entries)
        for idx in reversed(self._selected_positions()):
            if idx + 1 < ceil:
                entry = self.entries.pop(idx)
                self.entries.insert(idx + 1, entry)
                moves.append((entry["PlaylistItemId"], idx + 1))
                ceil = idx + 1
            else:
                ceil = idx
        self._apply_moves(moves)

    def _move_top(self):
        sel = self._selected_positions()
        if not sel:
            return
        picked = set(sel)
        block = [self.entries[i] for i in sel]
        rest = [e for i, e in enumerate(self.entries) if i not in picked]
        self.entries = block + rest
        # Applied in order, each move lands right after the previous one.
        self._apply_moves([(e["PlaylistItemId"], i)
                           for i, e in enumerate(block)])

    def _move_bottom(self):
        sel = self._selected_positions()
        if not sel:
            return
        picked = set(sel)
        block = [self.entries[i] for i in sel]
        rest = [e for i, e in enumerate(self.entries) if i not in picked]
        self.entries = rest + block
        last = len(self.entries) - 1
        self._apply_moves([(e["PlaylistItemId"], last) for e in block])

    def _remove_selected(self):
        sel = self._selected_positions()
        if not sel:
            return
        picked = set(sel)
        entry_ids = [self.entries[i].get("PlaylistItemId") for i in sel]
        entry_ids = [e for e in entry_ids if e]
        self.entries = [e for i, e in enumerate(self.entries)
                        if i not in picked]
        self._rebuild()
        if entry_ids:
            # One DELETE for the whole selection.
            self._send({"op": "remove", "entry_ids": entry_ids})

    def on_edit_result(self, result):
        result = result or {}
        if result.get("kind") != "playlist":
            return
        op = result.get("op")
        if result.get("ok"):
            if op == "delete":
                # The playlist no longer exists — leave the editor and its
                # detail view behind and drop back to the list.
                self.app.after_playlist_deleted(self.route["playlist_id"])
            return
        # The server refused (or the connection blipped): our local model is no
        # longer trustworthy — reload the real order, and for a rename/
        # visibility change re-sync the real value too.
        self._load()
        if op == "update":
            self._load_visibility()


class SearchView(BaseView):
    # Result sections in display order, like jellyfin-web's grouped search.
    GROUPS = [("Movie", _("Movies")), ("Series", _("Shows")),
              ("Episode", _("Episodes")), ("Video", _("Videos"))]

    def _build(self):
        server = self.app.current_server
        term = self.route.get("term", "")
        tk = self.app.tk
        tk.Label(self.frame, text=_('Results for "%s"') % term, bg=CARD_BG,
                 fg=TEXT_FG, font=("TkDefaultFont", 14, "bold")).pack(
            anchor="w", padx=8, pady=4)

        container = VScrollFrame(self.app, self.frame)
        container.widget().pack(fill="both", expand=True)
        body = container.body()
        spinner = self._spinner(body)

        def work():
            items = self.app.source.search(server, term)
            try:
                people = self.app.source.search_people(server, term)
            except Exception:
                log.debug("People search failed", exc_info=True)
                people = []
            return items, people

        def done(result):
            items, people = result
            spinner.destroy()
            if not items and not people:
                tk.Label(body, text=_("No results."), bg=CARD_BG,
                         fg=SUBTLE_FG).pack(pady=40)
                return
            by_type = {}
            for i in items:
                by_type.setdefault(i.get("Type"), []).append(i)
            shown = set()
            for type_key, title in self.GROUPS:
                group = by_type.get(type_key)
                if not group:
                    continue
                shown.add(type_key)
                if type_key == "Episode":
                    box = thumb_box(int(self.app.image_width * 1.4))
                    image_type = "Thumb"
                else:
                    box = poster_box(self.app.image_width)
                    image_type = "Primary"
                row = HScrollRow(self.app, body, title, box)
                row.widget().pack(fill="x")
                row.set_items(group, server, image_type=image_type,
                              on_click=self.app.open_item)
            other = [i for i in items if i.get("Type") not in shown]
            if other:
                row = HScrollRow(self.app, body, _("Other"),
                                 poster_box(self.app.image_width))
                row.widget().pack(fill="x")
                row.set_items(other, server, image_type="Primary",
                              on_click=self.app.open_item)
            self._render_people(body, people)

        self.run_async(work, done,
                           lambda e: (spinner.destroy(), self._error(self.frame, e)))

    def _render_people(self, body, people):
        if not people:
            return
        row = HScrollRow(self.app, body, _("People"),
                         poster_box(int(self.app.image_width * 0.7)))
        row.widget().pack(fill="x")
        tiles = []
        for p in people:
            entry = dict(p)
            entry["Type"] = "Person"  # /Persons items already carry ImageTags
            tiles.append(entry)
        row.set_items(tiles, self.app.current_server, image_type="Primary",
                      on_click=self._open_person, subtitle_fn=lambda _p: "")

    def _open_person(self, person):
        self.app.navigate({"kind": "grid", "person_id": person["Id"],
                           "title": person.get("Name", "")})


class DetailView(_DetailRowsMixin, BaseView):
    """Item detail with backdrop, metadata, resume/play, track pickers."""

    def __init__(self, app, route):
        super().__init__(app, route)
        self.item = None
        self.media_source = None
        self.audio_var = None
        self.sub_var = None
        self._audio_map = {}
        self._sub_map = {}
        self._actions_row = None
        self._download_btn = None

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

        self.run_async(work, done,
                           lambda e: (spinner.destroy(), self._error(self.frame, e)))

    def _render(self, item):
        tk = self.app.tk

        title = item.get("Name", "")
        if item.get("Type") == "Episode":
            s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
            if s is not None and e is not None:
                title = "%s — S%dE%d · %s" % (
                    item.get("SeriesName", ""), s, e, title)
        container = VScrollFrame(self.app, self.frame)
        container.widget().pack(fill="both", expand=True)
        outer = container.body()
        build_media_header(self.app, outer, item, title_text=title)

        body = tk.Frame(outer, bg=CARD_BG)
        body.pack(fill="x", padx=16, pady=8)

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
            self._build_version_picker(body, item)
        except Exception:
            log.warning("Version picker build failed", exc_info=True)
        self._media_info = tk.Label(body, text="", bg=CARD_BG, fg=SUBTLE_FG,
                                    anchor="w")
        self._media_info.pack(fill="x", pady=(0, 2))
        self._update_media_info()
        self._pickers_frame = tk.Frame(body, bg=CARD_BG)
        self._pickers_frame.pack(fill="x")
        try:
            self._build_track_pickers(self._pickers_frame)
        except Exception:
            # Never let a track-picker failure strand the page without a Play
            # button — fall back to default tracks.
            log.warning("Track picker build failed", exc_info=True)
        self._build_actions(body, item)
        self._build_people_row(outer, item)
        self._build_chapters_row(outer, item)
        self._load_similar_row(outer, item)
        self._load_trailer_button(item)

    def _pick_source(self, item):
        sources = item.get("MediaSources") or []
        return sources[0] if sources else None

    def _build_version_picker(self, parent, item):
        """Multiple MediaSources (4K vs 1080p, cuts) get an explicit picker;
        the play payload already carries media_source_id, it was just always
        MediaSources[0] before."""
        tk, ttk = self.app.tk, self.app.ttk
        sources = item.get("MediaSources") or []
        if len(sources) < 2:
            return
        row = tk.Frame(parent, bg=CARD_BG)
        row.pack(fill="x", pady=(0, 4))
        tk.Label(row, text=_("Version:"), bg=CARD_BG, fg=SUBTLE_FG).pack(
            side="left")
        self._source_map = {}
        labels = []
        for i, s in enumerate(sources):
            label = s.get("Name") or _("Version %d") % (i + 1)
            if label in self._source_map:
                label = "%s (%d)" % (label, i + 1)
            labels.append(label)
            self._source_map[label] = s
        self.version_var = tk.StringVar()
        box = ttk.Combobox(row, textvariable=self.version_var, state="readonly",
                           width=40, values=labels)
        box.set(labels[0])
        box.pack(side="left", padx=4)
        box.bind("<<ComboboxSelected>>", self._on_version_change)

    def _on_version_change(self, _e):
        src = getattr(self, "_source_map", {}).get(self.version_var.get())
        if src is None or src is self.media_source:
            return
        self.media_source = src
        # Track pickers list the chosen version's streams.
        self._audio_map, self._sub_map = {}, {}
        self.audio_var = self.sub_var = None
        for child in self._pickers_frame.winfo_children():
            child.destroy()
        try:
            self._build_track_pickers(self._pickers_frame)
        except Exception:
            log.warning("Track picker rebuild failed", exc_info=True)
        self._update_media_info()

    def _update_media_info(self):
        try:
            self._media_info.config(text=self._media_info_text())
        except Exception:
            log.debug("Media info render failed", exc_info=True)

    def _media_info_text(self):
        """Codec/resolution/size line plus 'Ends at', like jellyfin-web —
        useful for judging direct-play before hitting Play."""
        src = self.media_source or {}
        streams = src.get("MediaStreams") or []
        parts = []
        video = next((s for s in streams if s.get("Type") == "Video"), None)
        if video:
            bits = [(video.get("Codec") or "").upper()]
            if video.get("Width") and video.get("Height"):
                bits.append("%dx%d" % (video["Width"], video["Height"]))
            vrange = video.get("VideoRangeType") or video.get("VideoRange")
            if vrange and vrange != "SDR":
                bits.append(vrange)
            parts.append(" ".join(b for b in bits if b))
        audio = next((s for s in streams if s.get("Type") == "Audio"), None)
        if audio:
            parts.append(" ".join(b for b in (
                (audio.get("Codec") or "").upper(),
                audio.get("ChannelLayout") or "") if b))
        if src.get("Container"):
            parts.append(src["Container"])
        if src.get("Size"):
            parts.append(human_size(src["Size"]))
        if src.get("Bitrate"):
            parts.append("%.1f Mbps" % (src["Bitrate"] / 1_000_000))
        runtime = (self.item or {}).get("RunTimeTicks")
        if runtime:
            pos = ((self.item.get("UserData") or {})
                   .get("PlaybackPositionTicks") or 0)
            remaining = max(runtime - pos, 0) // 10_000_000
            ends = datetime.now() + timedelta(seconds=remaining)
            parts.append(_("Ends at %s") % ends.strftime("%H:%M"))
        return "  ·  ".join(p for p in parts if p)

    def _build_chapters_row(self, parent, item):
        chapters = item.get("Chapters") or []
        if len(chapters) < 2:
            return
        box = thumb_box(int(self.app.image_width * 1.1))
        row = HScrollRow(self.app, parent, _("Scenes"), box)
        row.widget().pack(fill="x")
        tiles = []
        for i, ch in enumerate(chapters):
            url = self.app.source.chapter_image_url(
                self.app.current_server, item["Id"], i, ch, width=box[0])
            tiles.append({
                "Id": "%s#ch%d" % (item["Id"], i),
                "Name": ch.get("Name") or _("Chapter %d") % (i + 1),
                "Type": "Chapter",
                "_start_ticks": ch.get("StartPositionTicks") or 0,
                # Chapter art isn't addressable via image_spec; hand the tile
                # a ready-made spec+url (None url -> placeholder, e.g. offline).
                "_image_spec": ((item["Id"], "Chapter%d" % i,
                                 ch.get("ImageTag") or "none")
                                if url else None),
                "_image_url": url,
            })
        row.set_items(tiles, self.app.current_server,
                      on_click=self._play_chapter,
                      subtitle_fn=lambda c: format_ticks(c["_start_ticks"]))

    def _play_chapter(self, chapter):
        self._play(chapter.get("_start_ticks") or 0)

    def _load_trailer_button(self, item):
        if item.get("Type") not in ("Movie", "Series"):
            return
        server = self.app.current_server

        def work():
            return self.app.source.get_trailers(server, item["Id"])

        def done(trailers):
            if not trailers or self._actions_row is None:
                return
            self._trailers = trailers

            def play_trailer():
                self.app.play({
                    "server_uuid": server,
                    "item_ids": [t["Id"] for t in self._trailers
                                 if t.get("Id")],
                    "start_index": 0,
                })

            try:
                self.app.ttk.Button(self._actions_row, text=_("🎬 Trailer"),
                                    command=play_trailer).pack(side="left",
                                                               padx=8)
            except Exception:
                pass  # actions row already torn down

        self.run_async(work, done, lambda _e: None)

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

        self._fav_state = bool((item.get("UserData") or {}).get("IsFavorite"))
        self._fav_btn = ttk.Button(row, text=self._fav_label(),
                                   command=self._toggle_favorite)
        self._fav_btn.pack(side="left", padx=8)

        self._actions_row = row
        self._download_btn = self._make_download_button(row, item)

    def _fav_label(self):
        return _("♥ Unfavorite") if self._fav_state else _("♡ Favorite")

    def _toggle_favorite(self):
        self._fav_state = not self._fav_state
        self.app.set_favorite(self.app.current_server, self.item["Id"],
                              self._fav_state)
        self.item.setdefault("UserData", {})["IsFavorite"] = self._fav_state
        self._fav_btn.config(text=self._fav_label())

    def _make_download_button(self, row, item):
        ttk = self.app.ttk
        if self.app.is_downloaded(item):
            btn = ttk.Button(row, text=_("🗑 Remove download"),
                             command=lambda: self.app.delete_download(
                                 item_id=item["Id"]))
        else:
            btn = ttk.Button(row, text=_("⬇ Download"),
                             command=lambda: self.app.open_download_dialog(
                                 self.app.current_server, item["Id"],
                                 item.get("Type"), item.get("Name", "")))
        btn.pack(side="right")
        return btn

    def on_sync_state(self, _ss):
        # Swap only the download button in place — a full re-render would reset
        # the track pickers and scroll on every queue change.
        if self.item is None or self._actions_row is None:
            return
        try:
            self._download_btn.destroy()
        except Exception:
            pass
        self._download_btn = self._make_download_button(self._actions_row, self.item)

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

        # One-click provisioning: servers already used by any user can fill the
        # URL (or jump straight into Quick Connect) so they aren't retyped.
        known = list(getattr(app, "known_servers", None) or [])
        if known:
            picker = tk.Frame(self.frame, bg=CARD_BG)
            picker.pack(fill="x", pady=(0, 10))
            tk.Label(picker, text=_("Previously added servers:"), bg=CARD_BG,
                     fg=SUBTLE_FG).pack(anchor="w")
            for ks in known:
                addr = ks.get("address")
                if not addr:
                    continue
                krow = tk.Frame(picker, bg=ENTRY_BG)
                krow.pack(fill="x", pady=2)
                tk.Label(krow, text=ks.get("name") or addr, bg=ENTRY_BG,
                         fg=TEXT_FG).pack(side="left", padx=8, pady=4)
                ttk.Button(krow, text=_("Quick Connect"),
                           command=lambda a=addr: self._use_known(a, quick=True)
                           ).pack(side="right", padx=4)
                ttk.Button(krow, text=_("Use"),
                           command=lambda a=addr: self._use_known(a)
                           ).pack(side="right", padx=4)

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

        # Quick Connect: an alternative for SSO/passwordless users. Only the
        # server URL is needed; the code is entered in another Jellyfin session.
        self.qc_button = ttk.Button(self.frame, text=_("Use Quick Connect"),
                                    command=self.start_quick_connect)
        self.qc_button.pack(pady=(0, 4))

        # Code/status area, shown only while a Quick Connect flow is active.
        self.qc_frame = tk.Frame(self.frame, bg=CARD_BG)
        tk.Label(self.qc_frame, text=_("Enter this code in your Jellyfin app\n"
                                       "(Settings → Quick Connect):"),
                 bg=CARD_BG, fg=SUBTLE_FG, justify="center").pack()
        self.qc_code = tk.Label(self.qc_frame, text=_("Starting…"), bg=CARD_BG,
                                fg=TEXT_FG, font=("TkDefaultFont", 20, "bold"))
        self.qc_code.pack(pady=6)
        self.qc_cancel = ttk.Button(self.qc_frame, text=_("Cancel"),
                                    command=self.cancel_quick_connect)
        self.qc_cancel.pack()

    def widget(self):
        return self.frame

    def _use_known(self, address, quick=False):
        self.server.set(address)
        if quick:
            self.start_quick_connect()

    def submit(self):
        server = self.server.get().strip()
        user = self.user.get().strip()
        if not server or not user:
            self.error.config(text=_("Server and username are required."))
            return
        self.error.config(text=_("Connecting…"))
        self._set_busy(True)
        self._on_submit({"server": server, "username": user, "password": self.pw.get()})

    def start_quick_connect(self):
        server = self.server.get().strip()
        if not server:
            self.error.config(text=_("Server URL is required for Quick Connect."))
            return
        self.error.config(text="")
        self._set_busy(True)
        self.qc_code.config(text=_("Starting…"))
        self.qc_frame.pack(pady=(4, 0))
        self.app.quick_connect(server)

    def cancel_quick_connect(self):
        self.app.quick_connect_cancel()
        self._reset()

    def on_quick_connect_code(self, result):
        code = result.get("code")
        if code:
            self.qc_code.config(text=code)

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.button.config(state=state)
        self.qc_button.config(state=state)

    def _reset(self):
        self.qc_frame.pack_forget()
        self._set_busy(False)

    def on_result(self, result):
        if result.get("ok"):
            self.error.config(text="")
            self.server.set("")
            self.user.set("")
            self.pw.set("")
        else:
            self.error.config(
                text=result.get("error") or _("Could not connect. Check your details."))
        self._reset()


class PinDialog:
    """Small modal that collects a PIN and hands it to a callback. Stays open
    until told to close, so the caller can report a wrong-PIN error inline."""

    def __init__(self, app, title, prompt, on_submit):
        self.app = app
        self.on_submit = on_submit
        tk, ttk = app.tk, app.ttk

        win = tk.Toplevel(app.root)
        self.win = win
        win.title(title)
        win.configure(bg=CARD_BG)
        win.transient(app.root)
        win.resizable(False, False)

        tk.Label(win, text=prompt, bg=CARD_BG, fg=TEXT_FG, wraplength=300,
                 justify="left").pack(anchor="w", padx=16, pady=(16, 8))
        self.pin = tk.StringVar()
        entry = ttk.Entry(win, textvariable=self.pin, show="*", width=20)
        entry.pack(padx=16, anchor="w")
        entry.bind("<Return>", lambda _e: self.submit())
        self.error = tk.Label(win, text="", bg=CARD_BG, fg="#e57373",
                              wraplength=300, justify="left")
        self.error.pack(anchor="w", padx=16, pady=(6, 0))

        btns = tk.Frame(win, bg=CARD_BG)
        btns.pack(fill="x", padx=16, pady=14)
        ttk.Button(btns, text=_("Cancel"), command=self.cancel).pack(side="right")
        ttk.Button(btns, text=_("OK"), style="Accent.TButton",
                   command=self.submit).pack(side="right", padx=8)
        win.protocol("WM_DELETE_WINDOW", self.cancel)
        try:
            entry.focus_set()
            win.grab_set()
        except Exception:
            pass

    def submit(self):
        pin = self.pin.get()
        if not pin:
            self.set_error(_("Enter a PIN."))
            return
        self.on_submit(pin)

    def set_error(self, text):
        try:
            self.error.config(text=text)
        except Exception:
            pass

    def cancel(self):
        if getattr(self.app, "_pin_dialog", None) is self:
            self.app._pin_dialog = None
        self.close()

    def close(self):
        try:
            self.win.grab_release()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass


class PinSetupDialog:
    """Set, change, or clear a user's parental-control PIN. Changing or clearing
    an existing PIN requires entering the current one."""

    def __init__(self, app, user, on_submit):
        # on_submit(new_pin, require_startup, current_pin)
        self.app = app
        self.user = user
        self.on_submit = on_submit
        self._locked = bool(user.get("locked"))
        tk, ttk = app.tk, app.ttk

        win = tk.Toplevel(app.root)
        self.win = win
        win.title(_("Change PIN") if self._locked else _("Set PIN"))
        win.configure(bg=CARD_BG)
        win.transient(app.root)
        win.resizable(False, False)

        tk.Label(win, text=_("PIN for %s") % user.get("name", ""), bg=CARD_BG,
                 fg=TEXT_FG, font=("TkDefaultFont", 13, "bold")).pack(
            anchor="w", padx=16, pady=(14, 8))

        self.current = tk.StringVar()
        self.new = tk.StringVar()
        self.confirm = tk.StringVar()
        rows = []
        if self._locked:
            rows.append((_("Current PIN"), self.current))
        rows.append((_("New PIN"), self.new))
        rows.append((_("Confirm PIN"), self.confirm))
        for label, var in rows:
            row = tk.Frame(win, bg=CARD_BG)
            row.pack(fill="x", padx=16, pady=3)
            tk.Label(row, text=label, bg=CARD_BG, fg=SUBTLE_FG, width=12,
                     anchor="w").pack(side="left")
            ttk.Entry(row, textvariable=var, show="*", width=18).pack(side="left")

        self.require_startup = tk.BooleanVar(value=bool(user.get("require_startup")))
        ttk.Checkbutton(win, text=_("Require this PIN at startup and when "
                                    "reopening the window"),
                        variable=self.require_startup).pack(anchor="w", padx=16,
                                                            pady=(8, 0))

        self.error = tk.Label(win, text="", bg=CARD_BG, fg="#e57373",
                              wraplength=320, justify="left")
        self.error.pack(anchor="w", padx=16, pady=(6, 0))

        btns = tk.Frame(win, bg=CARD_BG)
        btns.pack(fill="x", padx=16, pady=14)
        ttk.Button(btns, text=_("Cancel"), command=self.close).pack(side="right")
        ttk.Button(btns, text=_("Save"), style="Accent.TButton",
                   command=self._save).pack(side="right", padx=8)
        if self._locked:
            ttk.Button(btns, text=_("Remove PIN"),
                       command=self._remove).pack(side="left")
        win.protocol("WM_DELETE_WINDOW", self.close)
        try:
            win.grab_set()
        except Exception:
            pass

    def _save(self):
        new = self.new.get()
        if not new:
            self.error.config(text=_("Enter a new PIN."))
            return
        if new != self.confirm.get():
            self.error.config(text=_("PINs do not match."))
            return
        self.on_submit(new, self.require_startup.get(),
                       self.current.get() if self._locked else None)
        self.close()

    def _remove(self):
        # Clearing the PIN routes through set_pin with an empty PIN; the main
        # process still verifies the current PIN before removing it.
        self.on_submit("", False, self.current.get())
        self.close()

    def close(self):
        try:
            self.win.grab_release()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass


class ClosePreferenceDialog:
    """First-close prompt: minimize to tray (historical behaviour) or exit. The
    choice is persisted so it isn't asked again."""

    def __init__(self, app, on_choice):
        self.app = app
        self.on_choice = on_choice
        tk, ttk = app.tk, app.ttk

        win = tk.Toplevel(app.root)
        self.win = win
        win.title(_("Close Window"))
        win.configure(bg=CARD_BG)
        win.transient(app.root)
        win.resizable(False, False)

        tk.Label(win, text=_("When you close the window, what should happen?"),
                 bg=CARD_BG, fg=TEXT_FG, font=("TkDefaultFont", 13, "bold"),
                 wraplength=380, justify="left").pack(anchor="w", padx=16,
                                                      pady=(16, 6))
        tk.Label(win, text=_("Minimizing keeps the app running in the system "
                             "tray so it stays available as a cast target. You "
                             "can change this later in Settings → Interface."),
                 bg=CARD_BG, fg=SUBTLE_FG, wraplength=380,
                 justify="left").pack(anchor="w", padx=16)

        btns = tk.Frame(win, bg=CARD_BG)
        btns.pack(fill="x", padx=16, pady=16)
        ttk.Button(btns, text=_("Exit"),
                   command=lambda: self._choose(False)).pack(side="right")
        ttk.Button(btns, text=_("Minimize to Tray"), style="Accent.TButton",
                   command=lambda: self._choose(True)).pack(side="right", padx=8)
        # Dismissing the prompt aborts the close and asks again next time.
        win.protocol("WM_DELETE_WINDOW", self._cancel)
        try:
            win.grab_set()
        except Exception:
            pass

    def _choose(self, minimize):
        cb = self.on_choice
        self._close()
        cb(minimize)

    def _cancel(self):
        if getattr(self.app, "_close_dialog", None) is self:
            self.app._close_dialog = None
        self._close()

    def _close(self):
        try:
            self.win.grab_release()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass


class AddToDialog:
    """Pick (or create) a playlist/collection to add an item to. Adding a
    Series/Season id is fine — the server expands it to its children for
    playlists, and collections hold the series itself."""

    def __init__(self, app, item, kind):
        self.app = app
        self.item = item
        self.kind = kind  # "playlist" | "collection"
        self.choices = []
        tk, ttk = app.tk, app.ttk

        win = tk.Toplevel(app.root)
        self.win = win
        win.title(_("Add to playlist") if kind == "playlist"
                  else _("Add to collection"))
        win.configure(bg=CARD_BG)
        win.transient(app.root)
        win.minsize(380, 340)

        tk.Label(win, text=_('Add "%s" to:') % item.get("Name", ""),
                 bg=CARD_BG, fg=TEXT_FG, wraplength=340,
                 justify="left").pack(anchor="w", padx=16, pady=(14, 6))

        holder = tk.Frame(win, bg=CARD_BG)
        holder.pack(fill="both", expand=True, padx=16)
        self.listbox = tk.Listbox(holder, bg=ENTRY_BG, fg=TEXT_FG,
                                  selectbackground=ACCENT, activestyle="none",
                                  highlightthickness=0)
        scroll = ttk.Scrollbar(holder, orient="vertical",
                               command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True)
        self.listbox.insert("end", _("Loading…"))
        self.listbox.bind("<Double-Button-1>", lambda _e: self._add())

        new_row = tk.Frame(win, bg=CARD_BG)
        new_row.pack(fill="x", padx=16, pady=(8, 0))
        tk.Label(new_row, text=_("or type a name to create a new one:"),
                 bg=CARD_BG, fg=SUBTLE_FG).pack(anchor="w", pady=(0, 4))
        self.new_var = tk.StringVar()
        entry = ttk.Entry(new_row, textvariable=self.new_var)
        entry.pack(fill="x")
        entry.bind("<Return>", lambda _e: self._submit())

        # New playlists default to private (the Jellyfin API otherwise creates
        # them public/visible to every user). Collections have no such toggle.
        # The checkbox is shown only while a name is being typed, so it can't
        # be read as applying to "add to an existing playlist".
        self.private_var = tk.BooleanVar(value=True)
        self.private_chk = None
        if kind == "playlist":
            self.private_chk = ttk.Checkbutton(
                win, text=_("Private (only me) — uncheck to share with all "
                            "users"),
                variable=self.private_var)

        self.btns = tk.Frame(win, bg=CARD_BG)
        self.btns.pack(fill="x", padx=16, pady=12)
        ttk.Button(self.btns, text=_("Cancel"), command=self.close).pack(
            side="right")
        # One primary button whose label/action follows the text box: empty →
        # "Add" (to the selected playlist), non-empty → "Create new". This
        # avoids the dead "Add" click when nothing is selected but a name was
        # typed.
        self.action_btn = ttk.Button(self.btns, text=_("Add"),
                                      style="Accent.TButton",
                                      command=self._submit)
        self.action_btn.pack(side="right", padx=8)
        self.new_var.trace_add("write", self._sync_mode)
        self._sync_mode()
        win.protocol("WM_DELETE_WINDOW", self.close)

        server = app.current_server

        def work():
            if kind == "playlist":
                return app.source.get_playlists(server)
            return app.source.get_collections(server)

        def done(items):
            self.choices = items or []
            try:
                self.listbox.delete(0, "end")
                for c in self.choices:
                    self.listbox.insert("end", c.get("Name", "?"))
                if not self.choices:
                    self.listbox.insert(
                        "end", _("(none yet — create one below)"))
            except Exception:
                pass  # dialog closed while loading

        app.run_async(work, done, lambda _e: done([]))

    def _sync_mode(self, *_trace):
        """Follow the name box: with text, the primary button creates a new
        playlist/collection and (for playlists) the Private toggle is shown;
        empty, it adds to the highlighted existing one."""
        creating = bool(self.new_var.get().strip())
        self.action_btn.config(
            text=_("Create new") if creating else _("Add"))
        if self.private_chk is not None:
            if creating:
                self.private_chk.pack(anchor="w", padx=16, pady=(6, 0),
                                      before=self.btns)
            else:
                self.private_chk.pack_forget()

    def _submit(self):
        # A typed name means "create"; otherwise add to the selection.
        if self.new_var.get().strip():
            self._create()
        else:
            self._add()

    def _payload(self):
        return {"op": "add", "item_ids": [self.item["Id"]],
                "server_uuid": self.app.current_server}

    def _add(self):
        sel = self.listbox.curselection()
        if not sel or sel[0] >= len(self.choices):
            return
        choice = self.choices[sel[0]]
        payload = self._payload()
        if self.kind == "playlist":
            payload["playlist_id"] = choice["Id"]
            self.app.playlist_edit(payload)
        else:
            payload["collection_id"] = choice["Id"]
            self.app.collection_edit(payload)
        self.close()

    def _create(self):
        name = self.new_var.get().strip()
        if not name:
            return
        payload = self._payload()
        payload.update({"op": "create", "name": name})
        if self.kind == "playlist":
            payload["is_public"] = not self.private_var.get()
            self.app.playlist_edit(payload)
        else:
            self.app.collection_edit(payload)
        self.close()

    def close(self):
        if getattr(self.app, "_add_to_dialog", None) is self:
            self.app._add_to_dialog = None
        try:
            self.win.destroy()
        except Exception:
            pass


class SyncPlayDialog:
    """Join/leave SyncPlay groups without having to start a video first (the
    in-player menu remains for group creation and in-session control).

    Joining is fire-and-forget: the server answers over the websocket
    (GroupJoined → queue update), and the main process starts playback from
    that exactly as it does for an in-player join."""

    def __init__(self, app):
        self.app = app
        tk, ttk = app.tk, app.ttk

        win = tk.Toplevel(app.root)
        self.win = win
        win.title(_("SyncPlay"))
        win.configure(bg=CARD_BG)
        win.transient(app.root)
        win.minsize(380, 200)

        self.body = tk.Frame(win, bg=CARD_BG)
        self.body.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        self.status = tk.Label(self.body, text=_("Loading groups…"),
                               bg=CARD_BG, fg=SUBTLE_FG)
        self.status.pack(pady=20)

        btns = tk.Frame(win, bg=CARD_BG)
        btns.pack(fill="x", padx=16, pady=12)
        ttk.Button(btns, text=_("Close"), command=self.close).pack(side="right")
        ttk.Button(btns, text=_("Refresh"), command=self.refresh).pack(
            side="right", padx=8)
        win.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def refresh(self):
        self.app.request_syncplay_groups()

    def on_groups(self, payload):
        payload = payload or {}
        groups = payload.get("groups") or []
        current = payload.get("current")
        tk, ttk = self.app.tk, self.app.ttk
        try:
            for child in self.body.winfo_children():
                child.destroy()
        except Exception:
            return  # dialog already closed
        if current:
            row = tk.Frame(self.body, bg=CARD_BG)
            row.pack(fill="x", pady=(0, 10))
            tk.Label(row, text=_("In a SyncPlay group."), bg=CARD_BG,
                     fg=TEXT_FG).pack(side="left")
            ttk.Button(row, text=_("Leave group"),
                       command=self._leave).pack(side="right")
        if not groups:
            tk.Label(self.body,
                     text=_("No SyncPlay groups are active right now.\n"
                            "Create one from another Jellyfin client, or from "
                            "the in-player menu."),
                     bg=CARD_BG, fg=SUBTLE_FG, justify="left").pack(pady=14)
            return
        for g in groups:
            row = tk.Frame(self.body, bg=CARD_BG)
            row.pack(fill="x", pady=3)
            name = g.get("name") or _("Group")
            if g.get("server_name"):
                name = "%s — %s" % (name, g["server_name"])
            tk.Label(row, text=name, bg=CARD_BG, fg=TEXT_FG,
                     anchor="w").pack(side="left")
            participants = ", ".join(g.get("participants") or [])
            if participants:
                tk.Label(row, text=participants, bg=CARD_BG, fg=SUBTLE_FG,
                         anchor="w").pack(side="left", padx=8)
            if g.get("group_id") == current:
                tk.Label(row, text=_("Joined"), bg=CARD_BG,
                         fg=ACCENT).pack(side="right")
            else:
                ttk.Button(row, text=_("Join"), style="Accent.TButton",
                           command=lambda g=g: self._join(g)).pack(side="right")

    def _join(self, group):
        self.app.syncplay_join(group.get("server_uuid"), group.get("group_id"))
        self.close()

    def _leave(self):
        self.app.syncplay_leave()
        self.close()

    def close(self):
        if getattr(self.app, "_syncplay_dialog", None) is self:
            self.app._syncplay_dialog = None
        try:
            self.win.destroy()
        except Exception:
            pass


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

        # The top bar (and its user switcher) is hidden on this chrome-free
        # screen, so offer a way to switch users here too — otherwise a user
        # with no servers yet would be stranded on the login form.
        others = [u for u in self.app.users
                  if u["id"] != self.app.active_user_id]
        if others:
            switch_row = tk.Frame(wrap, bg=CARD_BG)
            switch_row.pack(pady=(0, 12))
            tk.Label(switch_row, text=_("Switch user:"), bg=CARD_BG,
                     fg=SUBTLE_FG).pack(side="left", padx=(0, 6))
            for u in others:
                label = ("\U0001F512 " if u.get("locked") else "") + u["name"]
                self.app.ttk.Button(
                    switch_row, text=label,
                    command=lambda usr=u: self.app.request_switch_user(usr)
                    ).pack(side="left", padx=2)

        tk.Label(wrap, text=_("Sign in to your Jellyfin server"), bg=CARD_BG,
                 fg=SUBTLE_FG).pack(pady=(0, 10))
        self.form = _ServerForm(self.app, wrap, self.app.add_server, _("Connect"))
        self.form.widget().pack()

    def on_server_result(self, result):
        # On success the incoming server list re-navigates to Home automatically.
        self.form.on_result(result)

    def on_quick_connect_code(self, result):
        self.form.on_quick_connect_code(result)


class LockedView(BaseView):
    """Startup gate for a PIN-locked active user: unlock with the PIN, or switch
    to a different (possibly unlocked) user instead."""

    def _build(self):
        tk, ttk = self.app.tk, self.app.ttk
        wrap = tk.Frame(self.frame, bg=CARD_BG)
        wrap.place(relx=0.5, rely=0.5, anchor="center")

        active = next((u for u in self.app.users
                       if u["id"] == self.app.active_user_id), None)
        name = active["name"] if active else ""

        tk.Label(wrap, text="\U0001F512", bg=CARD_BG, fg=TEXT_FG,
                 font=("TkDefaultFont", 40)).pack()
        tk.Label(wrap, text=_("%s is locked") % name, bg=CARD_BG, fg=TEXT_FG,
                 font=("TkDefaultFont", 16, "bold")).pack(pady=(4, 10))

        self.pin = tk.StringVar()
        entry = ttk.Entry(wrap, textvariable=self.pin, show="*", width=20)
        entry.pack()
        entry.bind("<Return>", lambda _e: self._unlock())
        self.error = tk.Label(wrap, text="", bg=CARD_BG, fg="#e57373",
                              wraplength=320, justify="center")
        self.error.pack(pady=(6, 0))
        ttk.Button(wrap, text=_("Unlock"), style="Accent.TButton",
                   command=self._unlock).pack(pady=10)

        others = [u for u in self.app.users if u["id"] != self.app.active_user_id]
        if others:
            tk.Label(wrap, text=_("or switch to another user:"), bg=CARD_BG,
                     fg=SUBTLE_FG).pack(pady=(12, 4))
            for u in others:
                label = ("\U0001F512 " if u.get("locked") else "") + u["name"]
                ttk.Button(wrap, text=label,
                           command=lambda usr=u: self.app.request_switch_user(usr)
                           ).pack(pady=2)
        try:
            entry.focus_set()
        except Exception:
            pass

    def _unlock(self):
        pin = self.pin.get()
        if not pin:
            self.error.config(text=_("Enter a PIN."))
            return
        self.app._send_switch(
            {"id": self.app.active_user_id, "name": "", "locked": True}, pin)

    def on_switch_result(self, result):
        if not (result or {}).get("ok"):
            self.error.config(
                text=(result or {}).get("error") or _("Incorrect PIN."))


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

        self._render_users(body, tk, ttk)

        # The servers list is scoped to the active user (switching users swaps
        # which servers are connected), so name the section after them.
        active_name = next((u["name"] for u in self.app.users
                            if u["id"] == self.app.active_user_id), None)
        servers_title = (_("Servers for %s") % active_name if active_name
                         else _("Servers"))
        tk.Label(body, text=servers_title, bg=CARD_BG, fg=TEXT_FG,
                 font=("TkDefaultFont", 16, "bold")).pack(anchor="w", padx=16,
                                                          pady=(18, 8))
        if not self.app.server_list:
            tk.Label(body, text=_("No servers configured yet."), bg=CARD_BG,
                     fg=SUBTLE_FG).pack(anchor="w", padx=16)
        for cred in self.app.server_list:
            row = tk.Frame(body, bg=ENTRY_BG)
            row.pack(fill="x", padx=16, pady=3)
            if not cred.get("connected"):
                status, color = _("Offline"), "#e57373"
            elif cred.get("casting", True):
                status, color = _("Connected"), "#7bd88f"
            else:
                # Browses fine, but isn't (yet) a usable cast/remote target.
                status, color = _("Connected (casting unavailable)"), "#e5c07b"
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

    def _render_users(self, body, tk, ttk):
        users = self.app.users
        tk.Label(body, text=_("Users"), bg=CARD_BG, fg=TEXT_FG,
                 font=("TkDefaultFont", 16, "bold")).pack(anchor="w", padx=16,
                                                          pady=(12, 4))
        tk.Label(body, text=_("Switch between local users. Each user has its "
                              "own servers and a separate device identity; a "
                              "locked user needs a PIN to switch to."),
                 bg=CARD_BG, fg=SUBTLE_FG, wraplength=560,
                 justify="left").pack(anchor="w", padx=16, pady=(0, 6))

        can_delete = len(users) > 1
        for u in users:
            is_active = u["id"] == self.app.active_user_id
            row = tk.Frame(body, bg=ENTRY_BG)
            row.pack(fill="x", padx=16, pady=3)
            label = ("\U0001F512 " if u.get("locked") else "") + u.get("name", "?")
            tk.Label(row, text=label, bg=ENTRY_BG, fg=TEXT_FG,
                     font=("TkDefaultFont", 11, "bold")).pack(side="left", padx=8,
                                                              pady=6)
            if is_active:
                tk.Label(row, text=_("active"), bg=ENTRY_BG,
                         fg="#7bd88f").pack(side="left", padx=8)
            if can_delete and not is_active:
                ttk.Button(row, text=_("Delete"),
                           command=lambda uid=u["id"]: self.app.delete_user(uid)
                           ).pack(side="right", padx=4)
            ttk.Button(row, text=(_("Change PIN") if u.get("locked")
                                  else _("Set PIN")),
                       command=lambda usr=u: self._set_pin(usr)
                       ).pack(side="right", padx=4)
            ttk.Button(row, text=_("Rename"),
                       command=lambda usr=u: self._rename(usr)
                       ).pack(side="right", padx=4)
            if not is_active:
                ttk.Button(row, text=_("Switch"),
                           command=lambda usr=u: self.app.request_switch_user(usr)
                           ).pack(side="right", padx=4)

        add_row = tk.Frame(body, bg=CARD_BG)
        add_row.pack(fill="x", padx=16, pady=(6, 4))
        self.new_user_var = tk.StringVar()
        entry = ttk.Entry(add_row, textvariable=self.new_user_var, width=24)
        entry.pack(side="left")
        entry.bind("<Return>", lambda _e: self._add_user())
        ttk.Button(add_row, text=_("Add User"),
                   command=self._add_user).pack(side="left", padx=6)

    def _add_user(self):
        name = self.new_user_var.get().strip()
        if name:
            self.app.add_user(name)
            self.new_user_var.set("")

    def _rename(self, user):
        from tkinter import simpledialog
        name = simpledialog.askstring(
            _("Rename User"), _("New name:"), parent=self.app.root,
            initialvalue=user.get("name", ""))
        if name and name.strip():
            self.app.rename_user(user["id"], name.strip())

    def _set_pin(self, user):
        PinSetupDialog(
            self.app, user,
            lambda pin, startup, current: self.app.set_user_pin(
                user["id"], pin, startup, current))

    def on_server_result(self, result):
        if self.form:
            self.form.on_result(result)

    def on_quick_connect_code(self, result):
        if self.form:
            self.form.on_quick_connect_code(result)


class LogsPanel:
    """Live application log viewer, embedded in the Settings notebook."""

    # Cap the widget like the backing log_lines deque (2000) so a long-lived
    # window doesn't grow the Text buffer without bound.
    _MAX_LINES = 2000

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
        self._trim()
        self.text.config(state="disabled")
        self.text.see("end")

    def _trim(self):
        # Drop oldest lines so the Text buffer stays bounded. index("end-1c")
        # reports the last character's "line.col"; its line number is the line
        # count. To remove the first N lines, delete "1.0".."(N+1).0".
        try:
            line_count = int(self.text.index("end-1c").split(".")[0])
            excess = line_count - self._MAX_LINES
            if excess > 0:
                self.text.delete("1.0", "%d.0" % (excess + 1))
        except Exception:
            pass


class DownloadsPanel:
    """Offline downloads management: disk usage, status/progress, remove."""

    # Coalesce a burst of sync_state pushes (a batch download fires one per
    # item) into at most one refresh per this interval, so the panel updates
    # steadily instead of thrashing.
    _REFRESH_COALESCE_MS = 300

    def __init__(self, app, parent):
        self.app = app
        self.container = VScrollFrame(app, parent)
        self.container.widget().pack(fill="both", expand=True)
        self._rows = {}  # item_id -> status Label
        self._epoch = 0  # supersedes an in-flight catalog read (see refresh)
        self._rendered = False
        self._refresh_after = None
        self.refresh()

    def _open_db(self):
        if not self.app.catalog_path:
            return None
        try:
            return SyncDB(self.app.catalog_path, read_only=True)
        except Exception:
            return None

    def refresh(self):
        # Reading the catalog can be slow on a large library, and on_sync_state
        # fires this on every item during a batch download — do the SyncDB read
        # off the Tk thread (via app.run_async) so the UI doesn't stutter.
        tk = self.app.tk
        body = self.container.body()
        self._epoch += 1
        epoch = self._epoch
        # Only show the placeholder on the first load. On a refresh, keep the
        # current rows (and self._rows, so on_download_progress keeps landing)
        # on screen until the fresh data arrives, then swap — no flicker, no
        # lost progress updates in the read window.
        if not self._rendered:
            for child in body.winfo_children():
                child.destroy()
            self._rows = {}
            tk.Label(body, text=_("Loading…"), bg=CARD_BG, fg=SUBTLE_FG).pack(
                anchor="w", padx=16, pady=12)

        def work():
            db = self._open_db()
            if db is None:
                return [], self.app.sync_total, [], {}
            try:
                return (db.list(), db.total_size(),
                        db.list_playlists(), db.playlist_ownership())
            finally:
                db.close()

        def done(result):
            # Drop the result if a newer refresh superseded this one, or the
            # panel's widgets were torn down (user navigated away).
            if epoch != self._epoch or not body.winfo_exists():
                return
            rows, total, playlists, ownership = result
            self._render(body, rows, total, playlists, ownership)
            self._rendered = True

        self.app.run_async(work, done, lambda _e: None)

    def _render(self, body, rows, total, playlists=(), ownership=None):
        tk = self.app.tk
        for child in body.winfo_children():
            child.destroy()
        self._rows = {}

        tk.Label(body, text=_("Downloads — %s used") % human_size(total),
                 bg=CARD_BG, fg=TEXT_FG, font=("TkDefaultFont", 16, "bold")).pack(
            anchor="w", padx=16, pady=(12, 8))
        if not rows:
            tk.Label(body, text=_("Nothing downloaded yet."), bg=CARD_BG,
                     fg=SUBTLE_FG).pack(anchor="w", padx=16)
            return

        # Items a playlist download owns are grouped under that playlist; items
        # downloaded another way (owned=0 / no playlist) keep their normal
        # Movies/Series grouping.
        ownership = ownership or {}
        pl_name = {p["playlist_id"]: p.get("name") or _("Playlist")
                   for p in playlists}
        pl_order, pl_groups, rest = [], {}, []
        for r in rows:
            pid = ownership.get(r["item_id"])
            if pid and pid in pl_name:
                if pid not in pl_groups:
                    pl_groups[pid] = []
                    pl_order.append(pid)
                pl_groups[pid].append(r)
            else:
                rest.append(r)

        for pid in pl_order:
            self._playlist_block(body, pid, pl_name[pid], pl_groups[pid])

        movies = [r for r in rest if not r.get("series_id")]
        series_order, series_map = [], {}
        for r in rest:
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

    def _playlist_block(self, body, playlist_id, name, rows):
        tk, ttk = self.app.tk, self.app.ttk
        size = sum(r.get("downloaded_bytes") or r.get("size_bytes") or 0
                   for r in rows)
        header = tk.Frame(body, bg=PANEL_BG)
        header.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(header,
                 text=_("Playlist: %s  ·  %d · %s") % (
                     name, len(rows), human_size(size)),
                 bg=PANEL_BG, fg=TEXT_FG, font=("TkDefaultFont", 12, "bold")).pack(
            side="left", padx=8, pady=4)
        ttk.Button(header, text=_("Remove downloads"),
                   command=lambda p=playlist_id: self.app.delete_download(
                       playlist_id=p)).pack(side="right", padx=4, pady=2)
        for row in rows:
            self._item_row(body, row, indent=24)

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
        # Leading-throttle: a batch download fires sync_state per item; schedule
        # one refresh and ignore further pushes until it runs.
        if self._refresh_after is not None:
            return
        self._refresh_after = self.app.root.after(
            self._REFRESH_COALESCE_MS, self._run_scheduled_refresh)

    def _run_scheduled_refresh(self):
        self._refresh_after = None
        # The panel may have been torn down while the refresh was scheduled.
        if not self.container.widget().winfo_exists():
            return
        self.refresh()


# Friendly, grouped settings (in-player-menu + README-documented keys). Anything
# not listed here shows under the auto-generated "Advanced" toggle.
SETTINGS_SECTIONS = [
    (_("Interface"), ["player_name", "enable_gui", "start_minimized",
                      "close_to_tray", "fullscreen",
                      "enable_osc", "raise_mpv", "check_updates", "notify_updates"]),
    (_("Playback"), ["auto_play", "always_transcode", "local_kbps",
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
    (_("Downloads"), ["sync_path", "prefer_downloaded"]),
]

# Settings that exist in the config but are intentionally not shown in the UI
# (e.g. audio_output is a legacy no-op that confuses users; close_prompt_shown
# is internal bookkeeping for the first-close prompt).
SETTINGS_HIDDEN = {"audio_output", "close_prompt_shown"}

# Friendlier labels than the auto-generated title-cased key.
SETTINGS_LABEL_OVERRIDES = {
    "sync_path": _("Download Folder"),
    "prefer_downloaded": _("Prefer Downloaded Copy"),
    "close_to_tray": _("Close to Tray (keep running)"),
}

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
    if key in SETTINGS_LABEL_OVERRIDES:
        return SETTINGS_LABEL_OVERRIDES[key]
    return " ".join(_ACRONYMS.get(w, w.capitalize()) for w in key.split("_"))


class SettingsPanel:
    """Generated config form: curated sections + an Advanced toggle for the rest."""

    def __init__(self, app, parent):
        self.app = app
        tk = app.tk
        self.vars = {}  # key -> (tk var, type)
        # The path the user asked to move to, held while the async move runs so
        # a mid-move settings_data (still carrying the old path) can't revert the
        # field under the user. Cleared when the move reports its outcome.
        self._pending_sync_path = None
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
                          if t != "skip" and k not in curated
                          and k not in SETTINGS_HIDDEN)
        self._section(self.adv_frame, _("Advanced"), advanced)

        self.save_row = tk.Frame(body, bg=CARD_BG)
        self.save_row.pack(fill="x", padx=16, pady=14)
        self.status = tk.Label(self.save_row, text="", bg=CARD_BG, fg=SUBTLE_FG)
        self.status.pack(side="right", padx=8)
        self.save_btn = ttk.Button(self.save_row, text=_("Save Settings"),
                                   style="Accent.TButton", command=self._save)
        self.save_btn.pack(side="left")
        # Shown (packed) only while a download-folder move is running.
        self.move_progress = ttk.Progressbar(self.save_row, mode="determinate",
                                             length=180, maximum=100)
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
            elif key == "sync_path":
                var = tk.StringVar(value="" if value is None else str(value))
                cell = tk.Frame(grid, bg=CARD_BG)
                cell.grid(row=row, column=1, sticky="ew", pady=3)
                cell.columnconfigure(0, weight=1)
                ttk.Entry(cell, textvariable=var).grid(row=0, column=0,
                                                       sticky="ew", padx=(0, 6))
                ttk.Button(cell, text=_("Browse…"), width=10,
                           command=lambda v=var: self._pick_folder(v)).grid(
                    row=0, column=1)
            else:
                var = tk.StringVar(value="" if value is None else str(value))
                ttk.Entry(grid, textvariable=var, width=34).grid(
                    row=row, column=1, sticky="w", pady=3)
            self.vars[key] = (var, vtype)

    def _pick_folder(self, var):
        from tkinter import filedialog
        current = var.get().strip() or None
        chosen = filedialog.askdirectory(
            parent=self.app.root, mustexist=False, initialdir=current,
            title=_("Choose download folder"))
        if chosen:
            var.set(chosen)

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
        # A download-folder change moves files in the main process and reports
        # back asynchronously via on_status; don't claim "Saved." prematurely.
        stored = self.app.settings_values.get("sync_path") or ""
        if "sync_path" in changes and changes["sync_path"] != stored:
            self._pending_sync_path = changes["sync_path"]
            self.status.config(text=_("Updating download folder…"), fg=SUBTLE_FG)
        else:
            self.status.config(text=_("Saved."), fg=SUBTLE_FG)

    def on_status(self, status):
        if not isinstance(status, dict):
            return
        self._end_folder_move()
        # Move finished — stop pinning the field and show the now-persisted path
        # (the resolved absolute path on success, the unchanged old one on fail).
        self._pending_sync_path = None
        if "sync_path" in self.vars and "sync_path" in self.app.settings_values:
            var, _t = self.vars["sync_path"]
            val = self.app.settings_values.get("sync_path")
            var.set("" if val is None else str(val))
        text = status.get("text", "")
        self.status.config(text=text,
                           fg=SUBTLE_FG if status.get("ok") else "#e06c6c")
        if status.get("restart") and text:
            try:
                from tkinter import messagebox
                messagebox.showinfo(_("Restart required"), text,
                                    parent=self.app.root)
            except Exception:
                log.debug("Restart prompt failed", exc_info=True)

    def on_folder_progress(self, payload):
        copied = payload.get("copied", 0) or 0
        total = payload.get("total", 0) or 0
        if not self.move_progress.winfo_ismapped():
            self.move_progress.pack(side="left", padx=8)
            self.save_btn.state(["disabled"])
        pct = 100 if total <= 0 else min(100, int(copied * 100 / total))
        self.move_progress["value"] = pct
        if total > 0:
            self.status.config(text=_("Moving downloads… %(pct)d%% "
                                      "(%(done)s / %(total)s)") % {
                "pct": pct, "done": human_size(copied),
                "total": human_size(total)}, fg=SUBTLE_FG)
        else:
            self.status.config(text=_("Moving downloads…"), fg=SUBTLE_FG)

    def _end_folder_move(self):
        if self.move_progress.winfo_ismapped():
            self.move_progress.pack_forget()
        self.save_btn.state(["!disabled"])

    def on_data(self, values):
        # Refresh the displayed values after a save round-trip (coercion may have
        # adjusted them).
        self.app.settings_values = values
        for key, (var, vtype) in self.vars.items():
            if key not in values:
                continue
            if key == "sync_path" and self._pending_sync_path is not None:
                continue  # a move is running — keep the user's chosen path shown
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

    def on_quick_connect_code(self, result):
        if self.servers_panel:
            self.servers_panel.on_quick_connect_code(result)

    def on_servers_changed(self, _server_list):
        if self.servers_panel:
            self.servers_panel.refresh()

    def on_users_changed(self, _users):
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

    def on_settings_status(self, status):
        if self.settings_panel:
            self.settings_panel.on_status(status)

    def on_folder_progress(self, payload):
        if self.settings_panel:
            self.settings_panel.on_folder_progress(payload)

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
        self._is_collection = item_type in ("Series", "Season", "Playlist")
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
            watched_label = (_("Include watched items") if item_type == "Playlist"
                             else _("Include watched episodes"))
            self.watched_chk = ttk.Checkbutton(
                win, text=watched_label,
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
    "playlist": PlaylistView,
    "playlist_edit": PlaylistEditView,
    "detail": DetailView,
    "search": SearchView,
    "login": LoginView,
    "locked": LockedView,
    "connecting": ConnectingView,
    "settings": SettingsView,
}
