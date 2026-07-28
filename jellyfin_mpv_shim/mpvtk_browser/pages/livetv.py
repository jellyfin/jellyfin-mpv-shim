"""The Live TV screen: Programs, Guide, Channels, Recordings, Schedule,
Series.

jellyfin-web's six Live TV tabs, in one page rather than six controllers,
because the shim's tabs share a route dict and a per-tab cache exactly as
the music library's do (``pages/music.py``). Each tab is a ``_load_*`` /
``_render_*`` pair; the dispatch tables below are the whole routing.

Two things are not local decisions and are worth knowing before changing
anything here:

* **The guide window is paged, not scrolled.** See ``guide_view`` for why.
  This page owns *which* window (date, time, channel page) and the guide
  component owns drawing it.
* **The preferences are jellyfin-web's**, stored in the DisplayPreferences
  document that client uses. See ``live_tv``. So the guide's settings dialog
  is editing something the web client will read back, which is why it saves
  through the repository rather than into ``conf.settings``.
"""

import datetime

from ...i18n import _
from ...mpvtk.widgets import (
    Box, Button, Column, Icon, Row, Spacer, Text, VScroll)
from .. import components, guide_view, live_tv, theme
from ..components import chrome, controls
from ..repository import CHANNEL_PAGE
from ..tile_renderer import GRID_GAP
from .base import Page

#: How far the day arrows step.
DAY = datetime.timedelta(days=1)


class LiveTvPage(Page):
    """The tabbed Live TV library."""

    kind = "livetv"

    TABS = (("programs", _("Programs")),
            ("guide", _("Guide")),
            ("channels", _("Channels")),
            ("recordings", _("Recordings")),
            ("schedule", _("Schedule")),
            ("series", _("Series")))

    DEFAULT_TAB = "programs"

    # -- load --------------------------------------------------------------

    def load(self, epoch):
        tab = self.route.get("_tab") or self.DEFAULT_TAB
        getattr(self, "_load_" + tab, self._load_programs)(epoch)

    def _put(self, key="_data"):
        """The usual "stash it on the route and repaint" completion."""
        route = self.route

        def done(value):
            route[key] = value
            route["_loading"] = False
        return done

    def _srv(self):
        return self.route.get("server") or self.ctx.server

    def _load_programs(self, epoch):
        source, srv = self.ctx.source, self._srv()
        self.route_async(lambda: source.get_program_sections(srv),
                         self._put(), epoch)

    def _load_channels(self, epoch):
        source, srv = self.ctx.source, self._srv()
        cats = self._categories()

        def work():
            prefs = source.get_live_tv_prefs(srv)
            items, total = source.get_channels(srv, prefs=prefs,
                                               categories=cats)
            return {"prefs": prefs, "items": items, "total": total}

        def done(data):
            route = self.route
            route["_prefs"] = data["prefs"]
            route["_data"] = data["items"]
            route["_total"] = data["total"]
            route["_loading"] = False

        self.route_async(work, done, epoch)

    def _load_guide(self, epoch):
        source, srv = self.ctx.source, self._srv()
        cats = self._categories()
        # Bound on the loop thread, like the music library's tab fetch: read
        # inside the worker they would resolve against whatever window the
        # user had paged to by the time it landed.
        page = int(self.route.get("_chan_page") or 0)
        start = self._window_start()
        # Always fetch the WIDEST window the grid can draw, whatever is on
        # screen right now. The visible width decides how many columns are
        # drawn, and fetching only those would make every resize a new guide
        # request — including one from inside build(), which would reload the
        # route mid-render. Drawing fewer columns from a wider fetch costs
        # nothing: row_segments clips.
        end = start + datetime.timedelta(
            minutes=live_tv.CELL_MINUTES * live_tv.MAX_CELLS)
        # The provider's date range only bounds the arrows, and it does not
        # move while you are looking at it — so it is read once and carried
        # across window moves rather than costing a round trip per press.
        # Kept OUTSIDE _data on purpose: _reload_tab drops that, which is
        # exactly what every window move does.
        known_info = self.route.get("_guide_info")

        def work():
            prefs = source.get_live_tv_prefs(srv)
            info = (known_info if known_info is not None
                    else source.get_guide_info(srv))
            channels, total = source.get_channels(
                srv, start_index=page * CHANNEL_PAGE, limit=CHANNEL_PAGE,
                prefs=prefs, categories=cats, add_current_program=False)
            programs = source.get_guide(
                srv, [c.get("Id") for c in channels], start, end,
                categories=cats,
                want_hd=bool((prefs.get("indicators") or {}).get("hd")))
            by_channel: dict = {}
            for program in programs:
                by_channel.setdefault(program.get("ChannelId"), []).append(
                    program)
            return {"prefs": prefs, "info": info, "channels": channels,
                    "total": total, "programs": by_channel,
                    "start": start, "end": end}

        def done(data):
            route = self.route
            route["_guide_info"] = data["info"]
            route["_data"] = data
            route["_loading"] = False

        self.route_async(work, done, epoch)

    def _load_recordings(self, epoch):
        source, srv = self.ctx.source, self._srv()

        def work():
            latest = source.get_recordings(srv, limit=24)
            # One extra request so the Latest row can mark what is still
            # taping. Cheap (it is a short list), and without it the row
            # shows the same tile for a programme that finished last week
            # and one that is half-written.
            active = {r.get("Id") for r in
                      source.get_recordings(srv, is_in_progress=True)}
            for item in latest:
                if item.get("Id") in active:
                    item["_recording"] = True
            return {"latest": latest,
                    "folders": source.get_recording_folders(srv)}

        self.route_async(work, self._put(), epoch)

    def _load_schedule(self, epoch):
        source, srv = self.ctx.source, self._srv()

        def work():
            return {"active": source.get_recordings(srv, is_in_progress=True),
                    "timers": source.get_timers(srv, is_active=False,
                                                is_scheduled=True)}

        self.route_async(work, self._put(), epoch)

    def _load_series(self, epoch):
        source, srv = self.ctx.source, self._srv()
        self.route_async(lambda: source.get_series_timers(srv),
                         self._put(), epoch)

    # -- render ------------------------------------------------------------

    def render(self, size):
        tab = self.route.get("_tab") or self.DEFAULT_TAB
        body = getattr(self, "_render_" + tab, self._render_programs)
        return Column([self._tab_bar(tab), body(size)], flex=1,
                      align="stretch")

    def _tab_bar(self, current):
        tabs = [Button(label, id="lttab-" + key,
                       bg=theme.ACCENT if key == current else theme.BUTTON_BG,
                       fg=(theme.ACCENT_FG if key == current
                           else theme.TEXT_FG),
                       on_click=lambda k=key: self._set_tab(k))
                for key, label in self.TABS]
        return Row(tabs, gap=8, pad=12, align="center")

    def _scroll(self, children, scroll_id, gap=16):
        """A stack of carousels, snapped to section tops.

        The same treatment the home screen gets, and for the same two
        reasons. These tabs are **bitmap-heavy** — one composited strip per
        section — so a continuous offset repositions every visible strip on
        every frame; and they are **long**: Programs is six carousels, and
        Schedule is one per day. Landing between two rows, with a caption
        band across the top of the window, is the state snapping avoids.

        The breakpoints are explicit content-y values rather than a uniform
        pitch, because the sections differ in height: an auto-shaped poster
        row is half as tall again as a landscape one (see ``_auto_row``), so
        a fixed step would drift out of alignment within two sections.
        """
        return VScroll(Column(children, pad=chrome.CONTENT_PAD, gap=gap,
                              align="stretch"),
                       id=scroll_id, flex=1,
                       offset=self.parked_scroll(scroll_id),
                       snaps=components.section_offsets(
                           children, gap, pad=chrome.CONTENT_PAD))

    # -- Programs ----------------------------------------------------------

    def _render_programs(self, size):
        rows = self.route.get("_data")
        if rows is None:
            return chrome.busy()
        if not rows:
            return chrome.error(_("No programs are listed right now."))
        return self._scroll([self._auto_row(row["title"], row["items"],
                                            "lt-" + row["key"])
                             for row in rows],
                            "livetv-programs")

    def _auto_row(self, title, items, row_id, on_click=None):
        """A row shaped by its own artwork, like jellyfin-web's.

        Its card builder resolves one shape per row from the items' median
        aspect ratio (see ``TileRenderer.auto_geom``), which is why the
        Programs screen shows posters for the film rows and landscape stills
        for the rest. Landscape is the fallback for a row where nothing
        carries a ratio — most guide entries have no art of their own.
        """
        art = self.ctx.art
        geom, image_type = art.tiles.auto_geom(items, default=art.geom_wide,
                                               default_type="Thumb")
        return art.tiles.tile_row(title, items, row_id, geom=geom,
                                  image_type=image_type, on_click=on_click)

    # -- Guide -------------------------------------------------------------

    def _window_start(self):
        """Where the visible window begins.

        Seeded from the clock (rounded down to the half hour, so what is on
        *now* is in the first column) and then moved only by the arrows —
        never re-seeded, or paging forward would snap back to now on the
        next repaint.
        """
        start = self.route.get("_start")
        if start is None:
            start = live_tv.floor_to_cell(live_tv.now())
            self.route["_start"] = start
        return start

    def _categories(self):
        """The guide's category filter. Session state, like jellyfin-web's
        ``categoryOptions`` — it is a way to look through a big line-up, not
        a preference worth persisting."""
        return tuple(self.route.get("_categories") or ())

    #: Vertical space the guide's own header (tab bar + controls + time
    #: header) takes out of the content area, for the row virtualizer.
    GUIDE_CHROME_H = 90

    def _render_guide(self, size):
        data = self.route.get("_data")
        art = self.ctx.art
        # How many 30-minute columns fit. Purely a render-time decision —
        # the fetch always covers MAX_CELLS, so a resize redraws rather than
        # re-requesting (see _load_guide).
        cells = live_tv.cells_for_width(
            max(200, size[0] - 2 * chrome.CONTENT_PAD - guide_view.CHANNEL_W))
        head = self._guide_controls(data, cells)
        if data is None:
            return Column([head, chrome.busy()], flex=1, align="stretch")
        start = data["start"]
        end = min(data["end"], start + datetime.timedelta(
            minutes=live_tv.CELL_MINUTES * cells))
        grid = guide_view.guide_grid(
            data["channels"], data["programs"], start, end,
            (size[0], max(120, size[1] - self.GUIDE_CHROME_H)), data["prefs"],
            art.tiles, art.scroll, "livetv-guide",
            on_program=self._open_program,
            offset=self.parked_scroll("livetv-guide"),
            on_scroll=lambda off, mx: art.scroll.on_scroll(
                "livetv-guide", off, mx))
        return Column([head, grid], flex=1, align="stretch", gap=6)

    def _guide_controls(self, data, cells):
        """Date/time/channel-page navigation above the grid.

        The time arrows step by one visible window, so "later" always means
        "the next screenful" however wide the window happens to be.
        """
        start = (data or {}).get("start") or self._window_start()
        step = datetime.timedelta(minutes=live_tv.CELL_MINUTES * cells)
        info = (data or {}).get("info") or {}
        total = (data or {}).get("total") or 0
        page = int(self.route.get("_chan_page") or 0)

        def nav(icon, node_id, delta, tip):
            return Button("", id=node_id, icon=icon, flat=True, icon_size=20,
                          tip=tip, on_click=lambda: self._move_window(delta,
                                                                      info))

        controls_row = [
            nav("keyboard_double_arrow_left", "lt-prevday", -DAY,
                _("Previous Day")),
            nav("chevron_left", "lt-prevwin", -step, _("Earlier")),
            Text("%s   %s" % (live_tv.fmt_day(start), live_tv.fmt_time(start)),
                 size=17, bold=True),
            nav("chevron_right", "lt-nextwin", step, _("Later")),
            nav("keyboard_double_arrow_right", "lt-nextday", DAY,
                _("Next Day")),
            controls.action_btn("schedule", _("Now"), "lt-now",
                                self._jump_to_now),
            Spacer(flex=1),
        ]
        if total > CHANNEL_PAGE:
            last = (total - 1) // CHANNEL_PAGE
            controls_row += [
                Text(_("Channels %(from)d-%(to)d of %(total)d") % {
                    "from": page * CHANNEL_PAGE + 1,
                    "to": min((page + 1) * CHANNEL_PAGE, total),
                    "total": total}, size=14, color=theme.SUBTLE_FG),
                Button("", id="lt-chanprev", icon="keyboard_arrow_up", flat=True,
                       icon_size=20, tip=_("Previous Channels"),
                       on_click=lambda: self._channel_page(page - 1, last)),
                Button("", id="lt-channext", icon="keyboard_arrow_down", flat=True,
                       icon_size=20, tip=_("More Channels"),
                       on_click=lambda: self._channel_page(page + 1, last)),
            ]
        controls_row.append(controls.action_btn(
            "settings", _("Guide Settings"), "lt-guidecfg",
            self._open_guide_settings))
        return Row(controls_row, gap=6, align="center",
                   pad=(chrome.CONTENT_PAD, 0))

    def _move_window(self, delta, info):
        target = live_tv.clamp_window(self._window_start() + delta, info,
                                      live_tv.MAX_CELLS)
        if target == self.route.get("_start"):
            return          # already against the end of the guide data
        self.route["_start"] = target
        self._reload_tab()

    def _jump_to_now(self):
        self.route["_start"] = live_tv.floor_to_cell(live_tv.now())
        self._reload_tab()

    def _channel_page(self, page, last):
        page = max(0, min(page, last))
        if page == int(self.route.get("_chan_page") or 0):
            return
        self.route["_chan_page"] = page
        # A new set of channels is a new list; a stale offset would show the
        # scroll parked deep inside rows that no longer exist.
        self.ctx.art.scroll.forget("livetv-guide")
        self._reload_tab()

    def _open_guide_settings(self):
        prefs = ((self.route.get("_data") or {}).get("prefs")
                 or self.route.get("_prefs"))
        if prefs is None:
            return          # still loading; nothing to edit yet
        self.ctx.dialogs.guide_settings(
            self._srv(), prefs, self._categories(), self._guide_settings_saved)

    def _guide_settings_saved(self, prefs, categories):
        self.route["_prefs"] = prefs
        self.route["_categories"] = tuple(categories)
        # Both the guide and the channel list are filtered/sorted by these,
        # and each caches its own fetch — so drop every tab's cache rather
        # than only the one on screen.
        self.route.pop("_tab_cache", None)
        self._reload_tab()

    # -- Channels ----------------------------------------------------------

    def _render_channels(self, size):
        items = self.route.get("_data")
        if items is None:
            return chrome.busy()
        if not items:
            return chrome.error(_("No channels available."))
        art = self.ctx.art
        return VScroll(
            Column(art.tiles.grid_of(items, "ltchan", size,
                                     geom=art.geom_square,
                                     scroll_id="livetv-channels"),
                   pad=chrome.CONTENT_PAD, gap=GRID_GAP),
            id="livetv-channels", flex=1,
            offset=self.parked_scroll("livetv-channels"),
            snap=art.geom_square.strip_h + GRID_GAP,
            snap_off=chrome.CONTENT_PAD,
            on_scroll=lambda off, mx: art.scroll.on_scroll(
                "livetv-channels", off, mx, self._channels_scrolled))

    def _channels_scrolled(self, offset, maximum):
        """Page the next block of channels in near the bottom. A tuner
        line-up routinely runs past the first page, and without this the
        list simply stopped there."""
        source, srv = self.ctx.source, self._srv()
        prefs = self.route.get("_prefs") or {}
        cats = self._categories()

        def put(route, items, total):
            route["_data"], route["_total"] = items, total

        self.ctx.art.pages.more(
            self.route, offset, maximum,
            lambda r: (r.get("_data") or [], r.get("_total") or 0),
            put,
            lambda start: source.get_channels(srv, start_index=start,
                                              prefs=prefs, categories=cats))

    # -- Recordings --------------------------------------------------------

    def _render_recordings(self, size):
        data = self.route.get("_data")
        if data is None:
            return chrome.busy()
        rows = []
        if data.get("latest"):
            rows.append(self._auto_row(_("Latest Recordings"),
                                       data["latest"], "lt-recent"))
        if data.get("folders"):
            rows.append(self._auto_row(_("Recording Folders"),
                                       data["folders"], "lt-recfolders"))
        if not rows:
            return chrome.error(_("Nothing has been recorded yet."))
        return self._scroll(rows, "livetv-recordings")

    # -- Schedule ----------------------------------------------------------

    def _render_schedule(self, size):
        data = self.route.get("_data")
        if data is None:
            return chrome.busy()
        blocks = []
        if data.get("active"):
            blocks.append(self._auto_row(_("Active Recordings"),
                                         data["active"], "lt-active"))
        for day, timers in self._group_by_day(data.get("timers") or []):
            blocks.append(self._auto_row(
                day, timers, "lt-timers-" + day.replace(" ", ""),
                on_click=self._open_timer))
        if not blocks:
            return chrome.error(_("Nothing is scheduled to record."))
        return self._scroll(blocks, "livetv-schedule")

    @staticmethod
    def _group_by_day(timers):
        """``[(day-label, [timer, ...])]`` in start order.

        jellyfin-web's ``getTimersHtml`` grouping. A flat list of upcoming
        recordings is unreadable past about a day — the same programme name
        appears three times and nothing says which showing is which.
        """
        groups: list = []
        for timer in timers:
            start = live_tv.parse_time(timer.get("StartDate"))
            label = live_tv.fmt_day(start) if start else _("Scheduled")
            if groups and groups[-1][0] == label:
                groups[-1][1].append(timer)
            else:
                groups.append((label, [timer]))
        return groups

    # -- Series ------------------------------------------------------------

    def _render_series(self, size):
        items = self.route.get("_data")
        if items is None:
            return chrome.busy()
        if not items:
            return chrome.error(_("No series are set to record."))
        art = self.ctx.art
        return VScroll(
            Column(art.tiles.grid_of(items, "ltseries", size,
                                     scroll_id="livetv-series",
                                     on_click=self._open_series_timer),
                   pad=chrome.CONTENT_PAD, gap=GRID_GAP),
            id="livetv-series", flex=1,
            offset=self.parked_scroll("livetv-series"),
            on_scroll=lambda off, mx: art.scroll.on_scroll("livetv-series",
                                                           off, mx))

    # -- actions -----------------------------------------------------------

    def _open_program(self, program):
        """Open a guide cell.

        Always the program page, even for something airing right now: it is
        one click from there to Watch, and it is the only place Record lives.
        jellyfin-web makes the same split (a dialog on desktop, immediate
        playback only in its TV layout, where there is nowhere to put the
        recording controls).
        """
        self.ctx.nav.navigate({
            "kind": "program", "server": self._srv(),
            "item_id": program.get("Id"),
            "channel_id": program.get("ChannelId"),
            "title": program.get("Name", ""),
            # The list DTO, so the page can draw immediately and refine when
            # the authoritative fetch (with the live timer state) lands.
            "_seed": program,
        })

    def _open_timer(self, timer):
        self.ctx.dialogs.timer_editor(self._srv(), timer, series=False,
                                      on_change=self._reload_tab)

    def _open_series_timer(self, timer):
        self.ctx.dialogs.timer_editor(self._srv(), timer, series=True,
                                      on_change=self._reload_tab)

    def _reload_tab(self):
        """Re-fetch the current tab, dropping its cached copy.

        Used after anything that changes what the tab should show — a timer
        cancelled, the guide window moved, the settings saved.
        """
        route = self.route
        cache = route.get("_tab_cache")
        if cache:
            cache.pop(route.get("_tab") or self.DEFAULT_TAB, None)
        for key in ("_data", "_total"):
            route.pop(key, None)
        route["_loading"] = False
        self.ctx.nav.reload(route)

    def _set_tab(self, tab):
        """Switch tab, keeping what each tab has already loaded.

        Per route dict, so it dies with the page and cannot go stale across
        a navigation — the same cache ``MusicLibraryPage`` keeps, and for the
        same reason: the guide fetch in particular is expensive enough that
        flipping to Channels and back must not pay for it twice.
        """
        route = self.route
        old = route.get("_tab") or self.DEFAULT_TAB
        if tab == old:
            return
        cache = route.setdefault("_tab_cache", {})
        if route.get("_data") is not None:
            cache[old] = (route["_data"], route.get("_total"))
        route["_tab"] = tab
        for key in ("_data", "_total"):
            route.pop(key, None)
        route["_loading"] = False
        self.ctx.art.pages.reset(route)
        # Each tab has its own scroller; a stale offset would virtualize the
        # wrong window and show a screenful of blank rows.
        self.ctx.art.scroll.forget("livetv-programs", "livetv-channels",
                                   "livetv-guide", "livetv-recordings",
                                   "livetv-schedule", "livetv-series")
        hit = cache.get(tab)
        if hit is not None:
            route["_data"], route["_total"] = hit
            self.ctx.run.bump()
            self.ctx.invalidate()
        else:
            self.ctx.nav.reload(route)


class ProgramPage(Page):
    """One guide entry: what it is, when it is on, and what to do about it.

    jellyfin-web's ``recordingcreator`` dialog as a page. A page rather than
    a dialog because this is also where "Watch" lives, and a modal that
    starts playback has to tear itself down over a window it no longer owns.
    """

    kind = "program"

    def load(self, epoch):
        source, srv = self.ctx.source, self._srv()
        program_id = self.route["item_id"]
        # Seed from the tile that was clicked so the page draws instantly;
        # the fetch below replaces it with the authoritative DTO, which is
        # the only one carrying live TimerId/SeriesTimerId/Status.
        if self.route.get("_data") is None and self.route.get("_seed"):
            self.route["_data"] = self.route["_seed"]
        self.route_async(lambda: source.get_live_program(srv, program_id),
                         self._done, epoch)

    def _done(self, item):
        # Keep the seed when the fetch came back empty: an expired guide
        # entry is still worth showing rather than replacing with an error.
        if item:
            self.route["_data"] = item
        self.route["_loading"] = False

    def _srv(self):
        return self.route.get("server") or self.ctx.server

    def render(self, size):
        item = self.route.get("_data")
        if item is None:
            return chrome.busy()
        tiles = self.ctx.art.tiles
        w = size[0]
        bw, bh = tiles.banner_box(w)
        title = live_tv.program_title(item)
        meta = self._meta(item)
        banner = tiles.backdrop_node(item, (bw, bh), "program-bd",
                                     title=title, meta=meta,
                                     context=item.get("ChannelName") or "")
        blocks = [banner]
        if isinstance(banner, Box):
            # No artwork (or still loading): the heading is not baked into
            # the bitmap, so draw it as text — same split as DetailPage.
            if item.get("ChannelName"):
                blocks.append(Text(item["ChannelName"], size=17,
                                   color=theme.SUBTLE_FG))
            blocks.append(Text(title, size=26, bold=True, wrap=True,
                               w=tiles.body_w(w)))
            if meta:
                blocks.append(Text(meta, size=18, color=theme.SUBTLE_FG))
        sub = item.get("EpisodeTitle")
        if sub:
            blocks.append(Text(sub, size=18))
        blocks.append(self._buttons(item))
        state = live_tv.timer_state(item)
        if state:
            blocks.append(Row([
                Icon(live_tv.STATE_ICONS[state], 18,
                     color=(theme.SUBTLE_FG if state == "series_inactive"
                            else theme.FAV_RED)),
                Text(self.STATE_LABELS[state](), size=15,
                     color=theme.SUBTLE_FG)], gap=6, align="center"))
        if item.get("Overview"):
            blocks.append(chrome.paragraph(item["Overview"], 18,
                                           tiles.body_w(w)))
        return VScroll(Column(blocks, pad=chrome.CONTENT_PAD, gap=16),
                       id="program", flex=1,
                       offset=self.parked_scroll("program"))

    #: Callables, not strings: these are translated and the module is
    #: imported before i18n is installed.
    STATE_LABELS = {
        "timer": lambda: _("Scheduled to record"),
        "recording": lambda: _("Recording now"),
        "series": lambda: _("Recording this series"),
        "series_inactive": lambda: _("This series is set to record, but this "
                                     "showing is not scheduled"),
    }

    @staticmethod
    def _meta(item):
        parts = [live_tv.air_time_label(item)]
        start = live_tv.parse_time(item.get("StartDate"))
        if start is not None:
            parts.insert(0, live_tv.fmt_day(start))
        for field, label in (("IsLive", _("Live")), ("IsPremiere", _("Premiere")),
                             ("IsRepeat", _("Repeat"))):
            if item.get(field):
                parts.append(label)
        if item.get("OfficialRating"):
            parts.append(str(item["OfficialRating"]))
        if item.get("Genres"):
            parts.append(", ".join(item["Genres"][:3]))
        return "   ·   ".join(p for p in parts if p)

    def _buttons(self, item):
        actions = self.ctx.actions
        server = self._srv()
        channel = item.get("ChannelId") or self.route.get("channel_id")
        state = live_tv.timer_state(item)
        btns = []
        if channel:
            # Airing now: watch it. Otherwise the same button tunes to the
            # channel anyway, which is what you want when a programme starts
            # in two minutes — so it is offered either way, only demoted.
            btns.append(controls.action_btn(
                "play_arrow",
                _("Watch") if live_tv.is_airing(item) else _("Watch Channel"),
                "pg-watch",
                lambda: actions.play_list([channel], server, 0),
                primary=live_tv.is_airing(item), size=18))
        if not actions.can_record():
            return Row(btns, gap=10)
        if state in ("timer", "recording"):
            btns.append(controls.action_btn(
                "cancel",
                _("Stop Recording") if state == "recording"
                else _("Do Not Record"),
                "pg-cancel",
                lambda: actions.cancel_timer(item.get("TimerId"), server,
                                             on_done=self._refresh),
                size=18))
        else:
            btns.append(controls.action_btn(
                "fiber_manual_record", _("Record"), "pg-record",
                lambda: actions.schedule_recording(item, server,
                                                   on_done=self._refresh),
                size=18))
        if item.get("IsSeries"):
            if state == "series" or item.get("SeriesTimerId"):
                btns.append(controls.action_btn(
                    "cancel", _("Cancel Series"), "pg-cancelseries",
                    lambda: actions.cancel_series_timer(
                        item.get("SeriesTimerId"), server,
                        on_done=self._refresh),
                    size=18))
                btns.append(controls.action_btn(
                    "settings", _("Series Options"), "pg-seriesopts",
                    lambda: self.ctx.dialogs.timer_editor(
                        server, {"Id": item.get("SeriesTimerId")}, series=True,
                        on_change=self._refresh),
                    size=18))
            else:
                btns.append(controls.action_btn(
                    "fiber_smart_record", _("Record Series"), "pg-recseries",
                    lambda: actions.schedule_recording(item, server,
                                                       series=True,
                                                       on_done=self._refresh),
                    size=18))
        return Row(btns, gap=10)

    def _refresh(self):
        """Re-read the program after a recording change.

        A re-read rather than an optimistic flip: the server decides whether
        a "record this" became a series rule, what the timer's id is, and
        whether the request was honoured at all — and every one of those
        changes which buttons this page should show.
        """
        self.route.pop("_data", None)
        self.route["_loading"] = False
        self.ctx.nav.reload(self.route)
