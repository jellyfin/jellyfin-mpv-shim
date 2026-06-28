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
        self.c = tk.Canvas(parent, width=size, height=size, highlightthickness=0,
                           bd=0, cursor="hand2")
        self._draw(BUTTON_BG)
        self.c.bind("<Button-1>", lambda _e: command())
        self.c.bind("<Enter>", lambda _e: self._draw(ACCENT))
        self.c.bind("<Leave>", lambda _e: self._draw(BUTTON_BG))

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

        w, h = box
        self.frame = tk.Frame(parent, bg=CARD_BG, bd=0, highlightthickness=0)
        self.canvas = tk.Canvas(self.frame, width=w, height=h, bg=PLACEHOLDER_BG,
                                highlightthickness=0, bd=0)
        self.canvas.pack()

        pct = played_percent(item)
        if pct:
            bar_w = int(w * pct / 100.0)
            self.canvas.create_rectangle(0, h - 4, w, h, fill="#444", width=0,
                                         tags="overlay")
            self.canvas.create_rectangle(0, h - 4, bar_w, h, fill=ACCENT, width=0,
                                         tags="overlay")
        if app.is_downloaded(item):
            self.canvas.create_oval(w - 28, 6, w - 6, 28, fill=ACCENT,
                                    outline="#101216", tags="overlay")
            self.canvas.create_text(w - 17, 16, text="✓", fill="#ffffff",
                                    font=("TkDefaultFont", 10, "bold"), tags="overlay")

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

    def load(self):
        if self._requested:
            return
        self._requested = True
        spec = self.app.source.image_spec(self.item, self.image_type, self.box[0])
        if not spec:
            return
        item_id, image_type, tag = spec
        w, h = self.box
        key = make_key(item_id, image_type, tag, w, h)
        url = self.app.source.image_url(self.server_uuid, item_id, image_type, tag,
                                        w, height=h, fill=True)
        self.app.thumbs.request(key, url, self.box, self._set_image)

    def _set_image(self, photo):
        try:
            w, h = self.box
            if log.isEnabledFor(logging.DEBUG) and (
                    photo.width() > w + 1 or photo.height() > h + 1):
                log.debug("[tile] image LARGER than box: img=%dx%d box=%dx%d item=%r",
                          photo.width(), photo.height(), w, h, self.item.get("Name"))
            self.canvas.delete("img")
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
        try:
            self.canvas.delete("img")
        except Exception:
            pass
        self._photo = None
        self._requested = False


class ScrollableGrid:
    """Vertically scrolling responsive grid of MediaTiles with lazy artwork.

    Each tile is its own canvas window item (not children of one big inner
    frame). Tk only maps the items currently in view — each at a small on-screen
    position — which sidesteps the X11 signed-16-bit window-position limit that
    otherwise corrupts scrolling past ~32767px (the "stops 2/3 down" bug on
    large libraries). Rows use a uniform measured height so positions are exact.

    Infinite scroll: set ``on_near_end`` and call ``append_items`` to add pages.
    """

    def __init__(self, app, parent, tile_box, gutter=16):
        self.app = app
        tk, ttk = app.tk, app.ttk
        self.tile_box = tile_box
        self.gutter = gutter
        self.cell_w = tile_box[0] + gutter
        self.row_h = tile_box[1] + gutter + 56  # provisional; remeasured on layout
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

    def set_items(self, items, server_uuid, image_type="Primary", on_click=None,
                  subtitle_fn=None):
        for t in self.tiles:
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
            tile = MediaTile(self.canvas, self.app, item, self._server, self.tile_box,
                             self._image_type, self._on_click, subtitle=sub)
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
        self._cols = max(1, width // self.cell_w)
        pad = self.gutter // 2
        for i, tile in enumerate(self.tiles):
            r, c = divmod(i, self._cols)
            self.canvas.coords(tile._win, pad + c * self.cell_w, pad + r * self.row_h)
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
                tile.load()
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
        self.canvas.bind("<Configure>", lambda _e: self._update_arrows())

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
                tile.load()
