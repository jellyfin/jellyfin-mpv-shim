"""Reusable Tkinter widgets for the library browser.

Tiles use a fixed-pixel ``Canvas`` for the artwork so layout is stable before
images arrive (a grey placeholder shows immediately). Grids and rows load
thumbnails lazily — only tiles within (or near) the viewport request artwork —
which keeps scrolling snappy on large libraries.

Scroll regions are recomputed authoritatively after every content change via a
deferred ``_settle`` (``update_idletasks`` then ``scrollregion = bbox``), so the
region always matches the laid-out content even on the partial final page.

Mouse-wheel: the app binds the wheel globally and walks up from the widget under
the pointer for a ``_wheel_scroll`` callable. Vertical regions register one;
horizontal carousels do NOT, so the wheel always scrolls the page vertically
(carousels use their ◀ ▶ buttons).
"""

import logging

from ..i18n import _
from .thumbnails import make_key
from .theme import (
    CARD_BG, PLACEHOLDER_BG, TEXT_FG, SUBTLE_FG, ACCENT, BUTTON_BG,
)

log = logging.getLogger("library_browser.layout")

# Extra pixels beyond the viewport to eagerly load (readahead).
READAHEAD_MARGIN_PX = 500
# Fraction of the scroll range from the end that triggers an infinite-scroll load.
NEAR_END_FRACTION = 0.8


def format_ticks(ticks):
    if not ticks:
        return ""
    seconds = int(ticks // 10_000_000)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)


def played_percent(item):
    data = item.get("UserData") or {}
    pct = data.get("PlayedPercentage")
    if pct:
        return float(pct)
    return None


def is_watched(item):
    """Whether an item counts as fully watched. Episodes/movies use the Played
    flag; series/seasons are watched when nothing is left unplayed."""
    data = item.get("UserData") or {}
    if data.get("Played"):
        return True
    if item.get("Type") in ("Series", "Season"):
        unplayed = data.get("UnplayedItemCount")
        return unplayed == 0
    return False


def human_size(num):
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return ("%d %s" % (num, unit)) if unit == "B" else ("%.1f %s" % (num, unit))
        num /= 1024
    return "%.1f TB" % num


def item_subtitle(item):
    itype = item.get("Type")
    if itype == "Episode":
        season = item.get("ParentIndexNumber")
        ep = item.get("IndexNumber")
        if season is not None and ep is not None:
            return "%s · S%dE%d" % (item.get("SeriesName", ""), season, ep)
        return item.get("SeriesName", "")
    if itype in ("Movie", "Series", "Video"):
        year = item.get("ProductionYear")
        return str(year) if year else ""
    return ""


class NavButton:
    """A small square, solid carousel nav button (overlaid on the row edge)."""

    def __init__(self, parent, app, glyph, command, size=38):
        tk = app.tk
        self._s = size
        self._glyph = glyph
        self._command = command
        self._repeat_after = None
        self.c = tk.Canvas(parent, width=size, height=size, highlightthickness=0,
                           bd=0, cursor="hand2")
        self._draw(BUTTON_BG)
        # Press pages once; holding it auto-repeats after a short delay so you
        # can scan a long carousel by holding the arrow.
        self.c.bind("<ButtonPress-1>", self._on_press)
        self.c.bind("<ButtonRelease-1>", lambda _e: self._cancel_repeat())
        self.c.bind("<Enter>", lambda _e: self._draw(ACCENT))
        self.c.bind("<Leave>", self._on_leave)

    def _on_press(self, _e=None):
        self._command()
        self._repeat_after = self.c.after(600, self._repeat)

    def _repeat(self):
        self._command()
        self._repeat_after = self.c.after(200, self._repeat)

    def _on_leave(self, _e=None):
        self._draw(BUTTON_BG)
        self._cancel_repeat()  # sliding off the button stops the repeat

    def _cancel_repeat(self):
        if self._repeat_after is not None:
            try:
                self.c.after_cancel(self._repeat_after)
            except Exception:
                pass
            self._repeat_after = None

    def _draw(self, fill):
        s = self._s
        self.c.delete("all")
        self.c.create_rectangle(0, 0, s, s, fill=fill, outline="#101216")
        self.c.create_text(s // 2, s // 2 - 1, text=self._glyph, fill="#ffffff",
                           font=("TkDefaultFont", 15, "bold"))

    def place(self, **kw):
        self.c.place(**kw)

    def place_forget(self):
        self.c.place_forget()


class MediaTile:
    """A clickable artwork tile with title/subtitle and an optional resume bar."""

    def __init__(self, parent, app, item, server_uuid, box, image_type,
                 on_click, subtitle=None):
        tk = app.tk
        self.app = app
        self.item = item
        self.server_uuid = server_uuid
        self.box = box
        self.image_type = image_type
        self._requested = False
        self._photo = None
        self._key = None

        w, h = box
        self.frame = tk.Frame(parent, bg=CARD_BG, bd=0, highlightthickness=0)
        self.canvas = tk.Canvas(self.frame, width=w, height=h, bg=PLACEHOLDER_BG,
                                highlightthickness=0, bd=0)
        self.canvas.pack()

        # Placeholder shown until art loads — and left in place for items the
        # server has no art for (a fresh Collection tile, most music). A muted
        # music note for audio, otherwise the item's initial, so a blank tile
        # never looks broken. Removed in _set_image once real art arrives.
        itype = item.get("Type")
        if itype in ("Audio", "MusicAlbum", "MusicArtist"):
            glyph = "♪"  # ♪
        else:
            name = (item.get("Name") or "").strip()
            glyph = name[0].upper() if name else "?"
        self.canvas.create_text(
            w // 2, h // 2, text=glyph, fill=SUBTLE_FG,
            font=("TkDefaultFont", max(16, h // 4)), tags=("placeholder",))

        pct = played_percent(item)
        if pct:
            bar_w = int(w * pct / 100.0)
            self.canvas.create_rectangle(0, h - 4, w, h, fill="#444", width=0,
                                         tags="overlay")
            self.canvas.create_rectangle(0, h - 4, bar_w, h, fill=ACCENT, width=0,
                                         tags="overlay")
        if app.is_downloaded(item):
            cx, cy = w - 17, 17
            self.canvas.create_oval(cx - 11, cy - 11, cx + 11, cy + 11, fill=ACCENT,
                                    outline="#101216", tags="overlay")
            # Draw the download arrow as one filled polygon (shaft + head). The ⬇
            # glyph (U+2B07) renders as tofu in the canvas font on Windows, and a
            # stroked line + arrowhead lands the head off the even-width shaft on
            # GDI. A polygon symmetric about cx scan-converts centered everywhere.
            self.canvas.create_polygon(
                cx - 1.5, cy - 7, cx + 1.5, cy - 7, cx + 1.5, cy + 1,
                cx + 6, cy + 1, cx, cy + 7, cx - 5, cy + 1, cx - 1.5, cy + 1,
                fill="#ffffff", outline="", tags="overlay")
        self._draw_watched_badge()

        self.title = tk.Label(self.frame, text=item.get("Name", ""), bg=CARD_BG,
                              fg=TEXT_FG, wraplength=w, justify="center",
                              font=("TkDefaultFont", 9))
        self.title.pack(fill="x")

        sub = subtitle if subtitle is not None else item_subtitle(item)
        if sub:
            tk.Label(self.frame, text=sub, bg=CARD_BG, fg=SUBTLE_FG, wraplength=w,
                     justify="center", font=("TkDefaultFont", 8)).pack(fill="x")

        for widget in (self.canvas, self.title, self.frame):
            widget.bind("<Button-1>", lambda _e: on_click(item))
            # Right-click (Button-2 on macOS) to mark watched/unwatched.
            widget.bind("<Button-3>", self._show_context_menu)
            widget.bind("<Button-2>", self._show_context_menu)

    def _draw_watched_badge(self):
        """Top-left check badge for watched items (download badge is top-right)."""
        self.canvas.delete("watched")
        if not is_watched(self.item):
            return
        self.canvas.create_oval(6, 6, 28, 28, fill=ACCENT, outline="#101216",
                                tags=("overlay", "watched"))
        self.canvas.create_text(17, 17, text="✓", fill="#ffffff",
                                font=("TkDefaultFont", 10, "bold"),
                                tags=("overlay", "watched"))

    def _show_context_menu(self, event):
        itype = self.item.get("Type")
        video_types = ("Movie", "Episode", "Series", "Season", "Video", "Audio")
        music_containers = ("MusicAlbum", "MusicArtist", "MusicGenre")
        if itype not in video_types and itype not in music_containers:
            return
        menu = self.app.tk.Menu(self.frame, tearoff=0)
        # Play / Add to queue for anything directly playable or a music
        # container (Play replaces the queue; Add to queue appends to it).
        playable = ("Movie", "Episode", "Video", "Audio", "MusicAlbum",
                    "MusicArtist", "MusicGenre")
        if itype in playable:
            menu.add_command(label=_("Play"),
                             command=lambda: self.app.play_item(self.item))
            menu.add_command(label=_("Add to queue"),
                             command=lambda: self.app.queue_item(self.item))
            menu.add_separator()
        if itype in video_types:
            watched = is_watched(self.item)
            menu.add_command(
                label=_("Mark unwatched") if watched else _("Mark watched"),
                command=lambda: self._toggle_watched(not watched))
        if itype != "MusicGenre":  # genres aren't a favoritable item
            fav = bool((self.item.get("UserData") or {}).get("IsFavorite"))
            menu.add_command(
                label=_("Remove from favorites") if fav
                else _("Add to favorites"),
                command=lambda: self._toggle_favorite(not fav))
        if (not self.app.is_offline
                and getattr(self.app, "edit_apis", False)):
            menu.add_separator()
            menu.add_command(
                label=_("Add to playlist…"),
                command=lambda: self.app.open_add_to_dialog(self.item,
                                                            "playlist"))
            if itype in video_types:
                menu.add_command(
                    label=_("Add to collection…"),
                    command=lambda: self.app.open_add_to_dialog(self.item,
                                                                "collection"))
        # Views can contribute context-specific actions (e.g. "Remove from
        # playlist" inside a playlist, "Remove from collection" in a BoxSet).
        extra = getattr(self.app.current_view, "tile_context_actions", None)
        if callable(extra):
            try:
                actions = extra(self.item) or []
            except Exception:
                actions = []
            if actions:
                menu.add_separator()
            for label, cb in actions:
                menu.add_command(label=label, command=cb)
        # NB: no grab_release() here. Releasing the grab immediately after
        # tk_popup breaks click-away dismissal on X11 (the menu would stay up
        # until you selected an item or navigated); tk_popup manages and drops
        # its own grab when the menu is unposted (selection / Escape / click
        # elsewhere).
        menu.tk_popup(event.x_root, event.y_root)

    def _toggle_watched(self, watched):
        item_id = self.item.get("Id")
        if not item_id:
            return
        self.app.set_watched(self.server_uuid, item_id, watched)
        # Optimistic: reflect it immediately without a full re-render.
        data = self.item.setdefault("UserData", {})
        data["Played"] = watched
        if self.item.get("Type") in ("Series", "Season"):
            # is_watched falls back to UnplayedItemCount for these types; a
            # stale 0 would redraw the badge as still-watched after unmarking.
            data["UnplayedItemCount"] = 0 if watched else 1
        self._draw_watched_badge()

    def _toggle_favorite(self, favorite):
        item_id = self.item.get("Id")
        if not item_id:
            return
        self.app.set_favorite(self.server_uuid, item_id, favorite)
        # Optimistic, like the watched toggle.
        self.item.setdefault("UserData", {})["IsFavorite"] = favorite

    def load(self):
        if self._requested:
            return
        self._requested = True
        # Synthetic items (e.g. chapter markers) carry their own image spec +
        # url because their artwork isn't addressable through image_spec.
        spec = self.item.get("_image_spec") or self.app.source.image_spec(
            self.item, self.image_type, self.box[0])
        if not spec:
            return
        item_id, image_type, tag = spec
        w, h = self.box
        key = make_key(item_id, image_type, tag, w, h)
        self._key = key
        if "_image_url" in self.item:
            url = self.item["_image_url"]
        else:
            url = self.app.source.image_url(self.server_uuid, item_id, image_type,
                                            tag, w, height=h, fill=True)
        if not url:
            # The server vanished from a rebuilt source (or offline art moved);
            # keep the placeholder.
            return
        self.app.thumbs.request(key, url, self.box, self._set_image)

    def _set_image(self, photo):
        try:
            w, h = self.box
            if log.isEnabledFor(logging.DEBUG) and (
                    photo.width() > w + 1 or photo.height() > h + 1):
                log.debug("[tile] image LARGER than box: img=%dx%d box=%dx%d item=%r",
                          photo.width(), photo.height(), w, h, self.item.get("Name"))
            self.canvas.delete("img")
            self.canvas.delete("placeholder")  # real art replaces the glyph
            self.canvas.create_image(w // 2, h // 2, image=photo, tags="img")
            self.canvas.tag_raise("overlay")  # keep resume bar / badge above art
            self._photo = photo  # keep a reference
        except Exception:
            pass

    def unload(self):
        """Release the artwork bitmap when scrolled far off-screen so memory
        doesn't grow without bound on a large library. The thumbnail store's
        cache makes the reload on scroll-back cheap; load() re-requests it."""
        if not self._requested:
            return
        # Cancel any still-pending fetch so a fast-scrolled backlog doesn't hold
        # up the next view's artwork.
        if self._key is not None:
            try:
                self.app.thumbs.cancel(self._key, self._set_image)
            except Exception:
                pass
        try:
            self.canvas.delete("img")
        except Exception:
            pass
        self._photo = None
        self._requested = False


class TrackRow(MediaTile):
    """A horizontal 'tabular' row for a music track — small album art, position,
    title/artist, duration — clickable to play. Subclasses MediaTile purely to
    reuse its lazy artwork load/unload; only the layout differs. Used by
    ScrollableGrid in list_mode."""

    def __init__(self, parent, app, item, server_uuid, box, image_type,
                 on_click, subtitle=None):
        tk = app.tk
        self.app = app
        self.item = item
        self.server_uuid = server_uuid
        self.box = box
        self.image_type = image_type
        self._requested = False
        self._photo = None
        self._key = None

        self.frame = tk.Frame(parent, bg=CARD_BG, bd=0, highlightthickness=0)
        w, h = box
        self.canvas = tk.Canvas(self.frame, width=w, height=h, bg=PLACEHOLDER_BG,
                                highlightthickness=0, bd=0)
        self.canvas.pack(side="left", padx=(8, 10), pady=3)
        self.canvas.create_text(w // 2, h // 2, text="♪", fill=SUBTLE_FG,
                                font=("TkDefaultFont", max(10, h // 3)),
                                tags=("placeholder",))
        if subtitle:  # playlist position, right-aligned like a track number
            tk.Label(self.frame, text=subtitle, bg=CARD_BG, fg=SUBTLE_FG,
                     width=3, anchor="e").pack(side="left", padx=(0, 8))
        dur = format_ticks(item.get("RunTimeTicks"))
        if dur:
            tk.Label(self.frame, text=dur, bg=CARD_BG, fg=SUBTLE_FG,
                     anchor="e").pack(side="right", padx=12)
        mid = tk.Frame(self.frame, bg=CARD_BG)
        mid.pack(side="left", fill="x", expand=True)
        tk.Label(mid, text=item.get("Name", ""), bg=CARD_BG, fg=TEXT_FG,
                 anchor="w").pack(fill="x")
        artists = ", ".join(item.get("Artists") or [])
        if artists:
            tk.Label(mid, text=artists, bg=CARD_BG, fg=SUBTLE_FG, anchor="w",
                     font=("TkDefaultFont", 8)).pack(fill="x")
        # Bind the whole row — every label/frame/canvas — so a click anywhere on
        # it plays (child labels would otherwise swallow the click).
        def _bind_all(w):
            w.bind("<Button-1>", lambda _e: on_click(item))
            w.bind("<Button-3>", self._show_context_menu)
            w.bind("<Button-2>", self._show_context_menu)
            for child in w.winfo_children():
                _bind_all(child)
        _bind_all(self.frame)


class ScrollableGrid:
    """Vertically scrolling responsive grid of MediaTiles with lazy artwork.

    Each tile is its own canvas window item (not children of one big inner
    frame). Tk only maps the items currently in view — each at a small on-screen
    position — which sidesteps the X11 signed-16-bit window-position limit that
    otherwise corrupts scrolling past ~32767px (the "stops 2/3 down" bug on
    large libraries). Rows use a uniform measured height so positions are exact.

    Infinite scroll: set ``on_near_end`` and call ``append_items`` to add pages.
    """

    def __init__(self, app, parent, tile_box, gutter=16, tile_cls=None,
                 list_mode=False):
        self.app = app
        tk, ttk = app.tk, app.ttk
        self.tile_box = tile_box
        self.gutter = gutter
        self.cell_w = tile_box[0] + gutter
        self.row_h = tile_box[1] + gutter + 56  # provisional; remeasured on layout
        # list_mode lays tiles out one-per-row full width (a tabular track
        # list); tile_cls swaps the tile widget (e.g. TrackRow). Both reuse the
        # same lazy artwork + scroll machinery.
        self._tile_cls = tile_cls or MediaTile
        self._list_mode = list_mode
        self.tiles = []
        self._cols = 0
        self.on_near_end = None
        self._near_end_armed = True
        self._server = None
        self._image_type = "Primary"
        self._on_click = None
        self._subtitle_fn = None

        self.outer = tk.Frame(parent, bg=CARD_BG)
        self.canvas = tk.Canvas(self.outer, bg=CARD_BG, highlightthickness=0, bd=0)
        self.scroll = ttk.Scrollbar(self.outer, orient="vertical",
                                    command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas._wheel_scroll = self._wheel
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def widget(self):
        return self.outer

    def rearm_near_end(self):
        """Re-enable the infinite-scroll trigger. _update_visible disarms it
        before each on_near_end call and only append_items re-arms it, so a
        failed page load would otherwise wedge loading permanently."""
        self._near_end_armed = True

    def set_items(self, items, server_uuid, image_type="Primary", on_click=None,
                  subtitle_fn=None):
        for t in self.tiles:
            try:
                t.unload()  # cancel any in-flight fetch for the outgoing tiles
            except Exception:
                pass
            try:
                self.canvas.delete(t._win)
            except Exception:
                pass
            t.frame.destroy()
        self.tiles = []
        self._server = server_uuid
        self._image_type = image_type
        self._on_click = on_click
        self._subtitle_fn = subtitle_fn
        self._near_end_armed = True
        self._cols = 0
        self.canvas.yview_moveto(0)
        self.append_items(items)

    def append_items(self, items):
        for item in items:
            sub = self._subtitle_fn(item) if self._subtitle_fn else None
            tile = self._tile_cls(self.canvas, self.app, item, self._server,
                                  self.tile_box, self._image_type,
                                  self._on_click, subtitle=sub)
            tile._win = self.canvas.create_window(0, 0, window=tile.frame,
                                                  anchor="nw")
            self.tiles.append(tile)
        self._near_end_armed = True
        self._relayout()

    def _wheel(self, units):
        self.canvas.yview_scroll(units, "units")  # _on_yscroll handles the rest

    def _on_yscroll(self, lo, hi):
        self.scroll.set(lo, hi)
        self.app.root.after_idle(self._update_visible)

    def _on_canvas_configure(self, _e=None):
        self._relayout()

    def _relayout(self):
        if not self.tiles:
            self._update_scrollregion()
            return
        try:
            width = self.canvas.winfo_width()
            self.canvas.update_idletasks()
        except Exception:
            return  # canvas destroyed (deferred relayout during teardown)
        if width <= 1:
            self.app.root.after(50, self._relayout)
            return
        # Uniform row height = tallest tile (covers 2-line titles); exact, so
        # positions are deterministic and never overlap.
        self.row_h = max((t.frame.winfo_reqheight() for t in self.tiles),
                         default=self.tile_box[1]) + self.gutter
        pad = self.gutter // 2
        self._cols = 1 if self._list_mode else max(1, width // self.cell_w)
        for i, tile in enumerate(self.tiles):
            r, c = divmod(i, self._cols)
            self.canvas.coords(tile._win, pad + c * self.cell_w, pad + r * self.row_h)
            if self._list_mode:
                # Stretch each row to the full viewport width so its columns
                # (title expands, duration right-aligns) lay out correctly.
                self.canvas.itemconfigure(tile._win, width=width - 2 * pad)
        self._update_scrollregion()
        self.app.root.after_idle(self._update_visible)

    def _update_scrollregion(self):
        try:
            n = len(self.tiles)
            rows = -(-n // self._cols) if self._cols else 0
            content_h = rows * self.row_h + self.gutter
            vh = self.canvas.winfo_height()
            w = self.canvas.winfo_width()
            # Clamp to the viewport so a short page can't over-scroll into blank.
            self.canvas.configure(scrollregion=(0, 0, w, max(content_h, vh)))
            self._log_layout(content_h, rows)
        except Exception:
            pass

    def _log_layout(self, content_h, rows):
        if not log.isEnabledFor(logging.DEBUG):
            return
        if content_h == getattr(self, "_last_log_h", None):
            return
        self._last_log_h = content_h
        try:
            tile_h = [t.frame.winfo_reqheight() for t in self.tiles]
            log.debug(
                "[grid] tiles=%d cols=%d rows=%d row_h=%d content_h=%d viewport_h=%d "
                "box=%s tile_reqh(min/max)=%s/%s",
                len(self.tiles), self._cols, rows, self.row_h, content_h,
                self.canvas.winfo_height(), self.tile_box,
                min(tile_h) if tile_h else 0, max(tile_h) if tile_h else 0)
        except Exception:
            pass

    def _update_visible(self):
        if not self.tiles or not self._cols:
            return
        try:
            top = self.canvas.canvasy(0)
            bottom = top + self.canvas.winfo_height()
        except Exception:
            return
        pad = self.gutter // 2
        # Keep a generous band loaded around the viewport; release bitmaps for
        # tiles well outside it so memory tracks the window, not scroll depth.
        unload_margin = READAHEAD_MARGIN_PX * 4
        for i, tile in enumerate(self.tiles):
            y = pad + (i // self._cols) * self.row_h
            if y + self.row_h >= top - READAHEAD_MARGIN_PX and \
                    y <= bottom + READAHEAD_MARGIN_PX:
                # One tile failing to resolve artwork must not abort the pass
                # for every tile after it (that wedges lazy-load + near-end).
                try:
                    tile.load()
                except Exception:
                    log.debug("Tile artwork load failed", exc_info=True)
            elif y + self.row_h < top - unload_margin or \
                    y > bottom + unload_margin:
                tile.unload()

        if self.on_near_end and self._near_end_armed:
            try:
                _first, last = self.canvas.yview()
            except Exception:
                return
            if last >= NEAR_END_FRACTION:
                self._near_end_armed = False
                self.on_near_end()


class VScrollFrame:
    """A vertically scrolling container you can pack arbitrary widgets into."""

    def __init__(self, app, parent):
        self.app = app
        tk, ttk = app.tk, app.ttk
        self.outer = tk.Frame(parent, bg=CARD_BG)
        self.canvas = tk.Canvas(self.outer, bg=CARD_BG, highlightthickness=0, bd=0)
        self.scroll = ttk.Scrollbar(self.outer, orient="vertical",
                                    command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=CARD_BG)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas._wheel_scroll = lambda u: self.canvas.yview_scroll(u, "units")
        self.inner.bind("<Configure>", lambda _e: self._settle())
        self.canvas.bind(
            "<Configure>",
            lambda _e: self.canvas.itemconfigure(self._win,
                                                 width=self.canvas.winfo_width()))

    def _settle(self):
        try:
            self.canvas.update_idletasks()
            kids = self.inner.winfo_children()
            # Use the actual laid-out bottom of the children, not bbox (which
            # reports the inner window's *requested* height and can drift).
            bottom = max((k.winfo_y() + k.winfo_height() for k in kids), default=0)
            width = self.canvas.winfo_width()
            vh = self.canvas.winfo_height()
            # Clamp to the viewport so a short page can't over-scroll into blank.
            bbox = (0, 0, width, max(bottom, vh))
            self.canvas.configure(scrollregion=bbox)
        except Exception:
            return
        if log.isEnabledFor(logging.DEBUG):
            if bottom != getattr(self, "_last_log_h", None):
                self._last_log_h = bottom
                try:
                    info = [(type(k).__name__, k.winfo_reqheight(),
                             k.winfo_height(), k.winfo_y()) for k in kids]
                    log.debug("[vscroll] scrollregion=%s canvas_h=%d inner_reqh=%d "
                              "content_bottom=%d children(name,req,actual,y)=%s", bbox,
                              self.canvas.winfo_height(),
                              self.inner.winfo_reqheight(), bottom, info)
                except Exception:
                    pass

    def widget(self):
        return self.outer

    def body(self):
        return self.inner


class HScrollRow:
    """A horizontally scrolling row of MediaTiles with square ◀ ▶ buttons
    overlaid on the edges. Does not capture the wheel (page scrolls vertically).
    """

    def __init__(self, app, parent, title, tile_box):
        self.app = app
        tk = app.tk
        self.tile_box = tile_box
        self.tiles = []

        self.outer = tk.Frame(parent, bg=CARD_BG)
        tk.Label(self.outer, text=title, bg=CARD_BG, fg=TEXT_FG, anchor="w",
                 font=("TkDefaultFont", 12, "bold")).pack(fill="x", padx=12,
                                                          pady=(10, 2))

        self.body = tk.Frame(self.outer, bg=CARD_BG)
        self.body.pack(fill="x")
        # Start with a placeholder height; sized to content once tiles are laid
        # out. NOT expand=True: expanding would let the canvas grab extra
        # vertical space, making the row's actual height exceed its requested
        # height — which desyncs the parent VScrollFrame's scrollregion.
        self.canvas = tk.Canvas(self.body, bg=CARD_BG, highlightthickness=0, bd=0,
                                height=tile_box[1] + 48)
        self.canvas.pack(fill="x")

        self.inner = tk.Frame(self.canvas, bg=CARD_BG)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self._on_configure())
        self.canvas.bind("<Configure>", lambda _e: self._on_canvas_configure())

        # Square nav buttons overlaid on the row edges (created after the canvas
        # so they stack above it).
        self.left = NavButton(self.body, app, "‹", lambda: self._page(-1))
        self.right = NavButton(self.body, app, "›", lambda: self._page(1))

    def widget(self):
        return self.outer

    def set_items(self, items, server_uuid, image_type="Primary", on_click=None,
                  subtitle_fn=None):
        for idx, item in enumerate(items):
            sub = subtitle_fn(item) if subtitle_fn else None
            tile = MediaTile(self.inner, self.app, item, server_uuid, self.tile_box,
                             image_type, on_click, subtitle=sub)
            padx = (10, 0) if idx == 0 else (14, 0)  # left gaps only, no trailing pad
            tile.frame.grid(row=0, column=idx, padx=padx, pady=2, sticky="n")
            self.tiles.append(tile)
        self.app.root.after_idle(self._settle)

    def _settle(self):
        # Shrink the row canvas to the actual content height (kills blank space
        # under the posters), update scroll bounds, then refresh arrows/visibility.
        try:
            self.canvas.update_idletasks()
            reqh = self.inner.winfo_reqheight()
            if reqh <= 1:
                # Tiles not measured yet — don't collapse the row; retry shortly.
                self.app.root.after(50, self._settle)
                return
            bbox = self.canvas.bbox("all") or (0, 0, 0, 0)
            self.canvas.configure(height=reqh, scrollregion=bbox)
        except Exception:
            return
        if log.isEnabledFor(logging.DEBUG) and reqh != getattr(self, "_last_log_h", None):
            self._last_log_h = reqh
            sample = self.tiles[0] if self.tiles else None
            log.debug("[hrow] canvas_height=%d bbox=%s tiles=%d box=%s "
                      "tile0_frame_reqh=%s tile0_img_h=%s", reqh, bbox,
                      len(self.tiles), sample.box if sample else None,
                      sample.frame.winfo_reqheight() if sample else None,
                      sample.canvas.winfo_height() if sample else None)
        self._update_visible()
        self._update_arrows()

    def _on_configure(self):
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all") or (0, 0, 0, 0))
        except Exception:
            pass
        self._update_arrows()

    def _page(self, direction):
        try:
            width = self.canvas.winfo_width()
        except Exception:
            width = 600
        cell = self.tile_box[0] + 14
        units = max(1, (width - cell) // cell) * direction
        self.canvas.xview_scroll(units, "units")
        self.app.root.after_idle(self._update_visible)
        self.app.root.after_idle(self._update_arrows)

    def _on_canvas_configure(self):
        # A canvas resize (e.g. window resize) can widen the viewport and reveal
        # tiles that were previously off-screen; load their artwork too, not just
        # refresh the arrows.
        self._update_arrows()
        self._update_visible()

    def _update_arrows(self):
        try:
            first, last = self.canvas.xview()
        except Exception:
            return
        scrollable = (last - first) < 0.999
        if scrollable and first > 0.001:
            self.left.place(relx=0.0, rely=0.5, x=2, anchor="w")
        else:
            self.left.place_forget()
        if scrollable and last < 0.999:
            self.right.place(relx=1.0, rely=0.5, x=-2, anchor="e")
        else:
            self.right.place_forget()

    def _update_visible(self):
        if not self.tiles:
            return
        try:
            left = self.canvas.canvasx(0) - READAHEAD_MARGIN_PX
            right = left + self.canvas.winfo_width() + 2 * READAHEAD_MARGIN_PX
        except Exception:
            return
        for tile in self.tiles:
            x = tile.frame.winfo_x()
            if x + tile.frame.winfo_width() >= left and x <= right:
                try:
                    tile.load()
                except Exception:
                    log.debug("Tile artwork load failed", exc_info=True)
