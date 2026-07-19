"""mpvtk demo: exercises tile strips, h/v scrolling, scrollbar, textbox
and dropdown against generated placeholder posters.

Tiles are NOT individual overlays: whole rows are composited into one
BGRA strip per row (captions, progress bars and unwatched badges baked
in — see README "z-order"), with ImageMap hit-regions for interaction.

Run:  python3 -m jellyfin_mpv_shim.mpvtk [--backend libmpv]
Selftest (headless-friendly, writes screenshots + assertions):
      python3 -m jellyfin_mpv_shim.mpvtk --selftest /tmp/outdir
"""

import colorsys
import logging
import os
import random
import threading
import time

from .app import MpvtkApp
from .rawimage import MemoryStore, cache_dir, write_bgra
from .widgets import (
    Box,
    Busy,
    Button,
    Checkbox,
    Column,
    Dialog,
    Dropdown,
    Float,
    HScroll,
    Icon,
    Image,
    ImageMap,
    Menu,
    Progress,
    Row,
    Slider,
    Spacer,
    Stack,
    Table,
    Text,
    TextBox,
    VScroll,
)

GRID_TOTAL = 400  # virtual grid entries (cycled library) for infinite scroll
WINDOW_AHEAD = 8  # materialized rows below the viewport
WINDOW_BEHIND = 4  # materialized rows above the viewport

log = logging.getLogger("mpvtk.demo")

TILE_W, TILE_H = 140, 200
TILE_GAP = 14
CAPTION_H = 44
STRIP_H = TILE_H + CAPTION_H

_ADJ = ["Amber", "Broken", "Crimson", "Distant", "Electric", "Frozen",
        "Gilded", "Hollow", "Iron", "Jade", "Kindred", "Lunar", "Midnight",
        "Neon", "Obsidian", "Painted", "Quiet", "Rusted", "Silent", "Zero"]
_NOUN = ["Meridian", "Harbor", "Signal", "Garden", "Empire", "Voyage",
         "Hour", "Protocol", "Summit", "Tide", "Circus", "Archive",
         "Frontier", "Lantern", "Orchard", "Paradox", "Quarry", "Relay",
         "Station", "Zephyr"]


def _titles(n):
    rnd = random.Random(7)
    seen = set()
    out = []
    while len(out) < n:
        t = "%s %s" % (rnd.choice(_ADJ), rnd.choice(_NOUN))
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _fonts():
    from PIL import ImageFont

    try:
        return {
            "num": ImageFont.truetype("DejaVuSans-Bold.ttf", 44),
            "label": ImageFont.truetype("DejaVuSans.ttf", 16),
            "cap": ImageFont.truetype("DejaVuSans.ttf", 15),
            "sub": ImageFont.truetype("DejaVuSans.ttf", 13),
            "badge": ImageFont.truetype("DejaVuSans-Bold.ttf", 14),
        }
    except OSError:
        f = ImageFont.load_default()
        return {k: f for k in ("num", "label", "cap", "sub", "badge")}


def make_library(n):
    """Generate n items with in-memory PIL posters + fake metadata."""
    from PIL import Image as PILImage, ImageDraw

    fonts = _fonts()
    rnd = random.Random(11)
    items = []
    for i, title in enumerate(_titles(n)):
        hue = (i * 47 % 360) / 360.0
        top = tuple(
            int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.55, 0.45)
        )
        bottom = tuple(
            int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.65, 0.20)
        )
        img = PILImage.new("RGB", (TILE_W, TILE_H))
        dr = ImageDraw.Draw(img)
        for y in range(TILE_H):
            f = y / TILE_H
            dr.line(
                [(0, y), (TILE_W, y)],
                fill=tuple(
                    int(a + (b - a) * f) for a, b in zip(top, bottom)
                ),
            )
        dr.text(
            (TILE_W // 2, TILE_H // 2 - 10),
            str(i + 1),
            font=fonts["num"],
            anchor="mm",
            fill=(240, 240, 240),
        )
        dr.rectangle(
            [0, 0, TILE_W - 1, TILE_H - 1], outline=(20, 20, 20)
        )
        items.append(
            {
                "idx": i,
                "poster": img,
                "title": title,
                "year": 1980 + rnd.randrange(45),
                # decorations exercised by strip baking:
                "progress": (rnd.random() if rnd.random() < 0.3 else 0.0),
                "badge": (rnd.randrange(2, 9) if rnd.random() < 0.2 else 0),
                "watched": False,
            }
        )
    return items


class StripStore:
    """Bakes a list of tiles into one BGRA strip file, content-keyed.

    Decorations (progress, badge, watched) are part of the key, so
    changing one re-composites that strip under a new filename — the
    renderer's overlay cache never sees stale content.

    The cache is LRU-bounded so a long browsing session doesn't grow
    the scratch dir without limit. Anything on screen was requested by
    the current build and is therefore most-recent; evictions only hit
    strips scrolled well out of the materialization window.
    """

    MAX_ENTRIES = 48  # ~6MB worst case each -> bounded footprint

    def __init__(self, cache_dir, mem_store=None):
        from collections import OrderedDict

        self.dir = cache_dir
        self.mem = mem_store  # MemoryStore for the libmpv backend
        self.fonts = _fonts()
        self._cache = OrderedDict()
        self._counter = 0
        self.hits = 0
        self.misses = 0

    def _ellipsize(self, dr, text, font, max_w):
        if dr.textlength(text, font=font) <= max_w:
            return text
        while text and dr.textlength(text + "…", font=font) > max_w:
            text = text[:-1]
        return text + "…"

    def strip(self, items):
        key = tuple(
            (it["idx"], round(it["progress"], 2), it["badge"],
             it["watched"])
            for it in items
        )
        hit = self._cache.get(key)
        if hit:
            self._cache.move_to_end(key)
            self.hits += 1
            return hit
        self.misses += 1
        t0 = time.perf_counter()
        log.info(
            "strip: composing %d tiles (cache %d hit / %d miss)…",
            len(items),
            self.hits,
            self.misses,
        )
        from PIL import Image as PILImage, ImageDraw

        n = len(items)
        iw = n * TILE_W + (n - 1) * TILE_GAP if n else 1
        img = PILImage.new("RGBA", (iw, STRIP_H), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        regions = []
        for col, it in enumerate(items):
            x = col * (TILE_W + TILE_GAP)
            img.paste(it["poster"], (x, 0))
            if it["watched"]:
                # watched checkmark chip, top-left
                dr.ellipse(
                    [x + 6, 6, x + 26, 26], fill=(40, 160, 70, 255)
                )
                dr.line(
                    [(x + 11, 16), (x + 15, 20), (x + 21, 11)],
                    fill=(255, 255, 255, 255),
                    width=2,
                )
            if it["badge"]:
                # unwatched-count badge, top-right
                bw = 26
                dr.rounded_rectangle(
                    [x + TILE_W - bw - 5, 5, x + TILE_W - 5, 25],
                    radius=6,
                    fill=(53, 100, 205, 255),
                )
                dr.text(
                    (x + TILE_W - 5 - bw / 2, 15),
                    str(it["badge"]),
                    font=self.fonts["badge"],
                    anchor="mm",
                    fill=(255, 255, 255),
                )
            if it["progress"] > 0:
                # resume progress bar along the poster bottom
                dr.rectangle(
                    [x, TILE_H - 6, x + TILE_W - 1, TILE_H - 1],
                    fill=(0, 0, 0, 200),
                )
                dr.rectangle(
                    [
                        x,
                        TILE_H - 6,
                        x + int((TILE_W - 1) * it["progress"]),
                        TILE_H - 1,
                    ],
                    fill=(53, 100, 205, 255),
                )
            title = self._ellipsize(
                dr, it["title"], self.fonts["cap"], TILE_W
            )
            dr.text(
                (x, TILE_H + 6),
                title,
                font=self.fonts["cap"],
                fill=(204, 204, 204),
            )
            dr.text(
                (x, TILE_H + 26),
                str(it["year"]),
                font=self.fonts["sub"],
                fill=(136, 136, 136),
            )
            regions.append(
                {"x": x, "y": 0, "w": TILE_W, "h": STRIP_H, "idx": it["idx"]}
            )
        t_compose = time.perf_counter()
        if self.mem is not None:
            src, _, _ = self.mem.add(img)
        else:
            self._counter += 1
            src = os.path.join(
                self.dir, "strip%d.bgra" % self._counter
            )
            write_bgra(img, src)
        t_out = time.perf_counter()
        log.info(
            "strip: %d tiles (%dx%d) done — draw %.1fms, %s %.1fms",
            len(items),
            iw,
            STRIP_H,
            (t_compose - t0) * 1000,
            "to-memory" if self.mem is not None else "to-file",
            (t_out - t_compose) * 1000,
        )
        entry = {"src": src, "iw": iw, "ih": STRIP_H, "regions": regions}
        self._cache[key] = entry
        while len(self._cache) > self.MAX_ENTRIES:
            _, old = self._cache.popitem(last=False)
            if self.mem is not None:
                self.mem.remove(old["src"])
            else:
                try:
                    os.remove(old["src"])
                except OSError:
                    pass
        return entry


class Demo:
    def __init__(self, backend="jsonipc"):
        self.cache = cache_dir("mpvtk-demo-")
        self.items = make_library(40)
        self.app = MpvtkApp(backend=backend)
        self.strips = StripStore(
            self.cache,
            mem_store=MemoryStore() if self.app.in_process else None,
        )
        self.query = ""
        self.sort = "Name"
        self.status = "Hover and click tiles; scroll rows and the page."
        self.grid_window = (0, WINDOW_AHEAD + 4)  # materialized row range
        self._grid_geom = None  # (grid_top, pitch, cols, total_rows)
        self.menu = None  # {"idx": item, "x":, "y":} while a menu is open
        # widgets/logs test pages
        self.page = "browse"
        self.opts = {"autoplay": True, "transcode": False,
                     "subtitles": True}
        self.volume = 40.0
        self.progress = 0.0  # animated by a background thread
        self.password = ""
        self.sel_text = "select me and type"
        self.table_rows = [
            {"num": i + 1, "title": it["title"], "year": it["year"]}
            for i, it in enumerate(self.items[:10])
        ]
        self.table_sel = None
        self.table_multi = set()  # multi-select via shift/ctrl clicks
        self.hold_count = 0  # incremented by the hold-repeat button
        self._badge = None  # lazy bitmap floated over a strip (Stack)
        self.dialog_open = False
        self.toast = None
        self._toast_timer = None
        self.log_lines = [
            "%05d  %s  %s" % (i, ("INFO", "DEBUG", "WARN")[i % 3],
                              "demo log line about %s" % _titles(1)[0])
            for i in range(300)
        ]

    # ------------------------------------------------------------- state

    def _filtered(self):
        items = [
            it
            for it in self.items
            if self.query.lower() in it["title"].lower()
        ]
        if self.sort == "Name":
            items.sort(key=lambda it: it["title"])
        elif self.sort == "Year":
            items.sort(key=lambda it: it["year"])
        return items

    def _pick(self, idx):
        it = self.items[idx]
        self.status = "Selected: %s (%d)" % (it["title"], it["year"])
        self.app.invalidate()

    def _on_query(self, value):
        self.query = value
        self.app.invalidate()

    def _on_sort(self, index, value):
        self.sort = value
        self.app.invalidate()

    # ------------------------------------------------------ context menu

    def _open_menu(self, idx, x, y):
        self.menu = {"idx": idx, "x": x, "y": y}
        self.app.invalidate()

    def _close_menu(self):
        self.menu = None
        self.app.invalidate()

    def _menu_action(self, index, value):
        it = self.items[self.menu["idx"]]
        if value == "Mark Watched":
            # mutating decorations recomposites the affected strips
            it["watched"] = True
            it["badge"] = 0
            it["progress"] = 0.0
        self.status = "%s: %s" % (value, it["title"])
        self._close_menu()

    # ---------------------------------------------- widgets-page state

    def _set_page(self, page):
        self.page = page
        self.app.invalidate()

    def _toggle(self, key):
        self.opts[key] = not self.opts[key]
        self.app.invalidate()

    def _on_volume(self, value):
        self.volume = value
        self.app.invalidate()

    def _dlg_close(self):
        self.dialog_open = False
        self.app.invalidate()

    def _dlg_confirm(self):
        self.status = "Confirmed dialog action"
        self._dlg_close()

    def _show_toast(self):
        self.toast = "Marked as watched — this toast auto-dismisses"
        if self._toast_timer:
            self._toast_timer.cancel()

        def clear():
            self.toast = None
            self.app.invalidate()  # thread-safe: fires from the timer

        self._toast_timer = threading.Timer(2.5, clear)
        self._toast_timer.daemon = True
        self._toast_timer.start()
        self.app.invalidate()

    def _tbl_select(self, i, mods=None):
        # plain: select; shift: range from anchor; ctrl: toggle
        mods = mods or {}
        if mods.get("shift") and self.table_sel is not None:
            lo, hi = sorted((self.table_sel, i))
            self.table_multi = set(range(lo, hi + 1))
        elif mods.get("ctrl"):
            self.table_multi ^= {i}
            self.table_sel = i
        else:
            self.table_sel = i
            self.table_multi = {i}
        self.app.invalidate()

    def _tbl_move(self, d):
        i = self.table_sel
        if i is None:
            return
        j = i + d
        if 0 <= j < len(self.table_rows):
            rows = self.table_rows
            rows[i], rows[j] = rows[j], rows[i]
            self.table_sel = j
            self.table_multi = {j}
            self.app.invalidate()

    def _hold_tick(self):
        self.hold_count += 1
        self.app.invalidate()

    def _tbl_activate(self, i):
        self.status = "Activated %s" % self.table_rows[i]["title"]
        self.app.invalidate()

    def _progress_animator(self):
        # background thread driving the determinate progress bar —
        # live proof that invalidate() repaints without user input
        while True:
            time.sleep(0.12)
            if self.page == "widgets":
                self.progress = (self.progress + 2.5) % 100.0
                self.app.invalidate()

    # ------------------------------------------------------------- build

    def _image_map(self, items, id_prefix):
        s = self.strips.strip(items)
        regions = [
            dict(
                r,
                id="%s-tile-%d" % (id_prefix, r["idx"]),
                on_click=lambda i=r["idx"]: self._pick(i),
                on_context=lambda x, y, i=r["idx"]: self._open_menu(
                    i, x, y
                ),
            )
            for r in s["regions"]
        ]
        return ImageMap(
            s["src"], s["iw"], s["ih"], regions=regions,
            id="%s-img" % id_prefix,
        )

    def _badge_img(self):
        """Small solid dot floated over the first strip (Stack demo):
        bitmap-over-bitmap needs paint-ordered overlay slots."""
        if self._badge is None:
            from PIL import Image as PILImage, ImageDraw

            img = PILImage.new("RGBA", (26, 26), (0, 0, 0, 0))
            ImageDraw.Draw(img).ellipse(
                [1, 1, 24, 24], fill=(224, 64, 160, 255)
            )
            if self.strips.mem is not None:
                src, _, _ = self.strips.mem.add(img)
            else:
                src = os.path.join(self.cache, "zbadge.bgra")
                write_bgra(img, src)
            self._badge = (src, 26, 26)
        return self._badge

    def _row_section(self, heading, items, row_id):
        im = self._image_map(items, row_id)
        if row_id == "row-cw":
            # Stack demo: a bitmap badge over the strip (overlay slots
            # follow paint order) + an ASS chip punched through it
            # (occlude=True subtracts its rect from the image below)
            src, bw, bh = self._badge_img()
            im = Stack(
                [
                    im,
                    Image(src, bw, bh, id="zbadge",
                          anchor="nw", dx=TILE_W - 34, dy=8),
                    Box(
                        [Text("STACK", size=13, color="101010")],
                        id="zocc",
                        bg="ffcc66",
                        radius=4,
                        pad=4,
                        direction="row",
                        anchor="sw",
                        dx=8,
                        dy=-8,
                        occlude=True,
                    ),
                ],
                w=im.iw,
                h=im.ih,
            )
        return Column(
            [
                Text(heading, size=24, bold=True),
                HScroll(
                    im,
                    id=row_id,
                    h=STRIP_H + 6,
                ),
            ],
            gap=8,
        )

    def _grid_section(self, heading, items, width, grid_top):
        """Windowed 'infinite' grid: GRID_TOTAL virtual entries (library
        cycled), only rows near the viewport materialized; spacers stand
        in for the rest so scrollbar and offsets see the full height."""
        cols = max(
            1, int((width - 48 + TILE_GAP) // (TILE_W + TILE_GAP))
        )
        gap = 12
        pitch = STRIP_H + gap
        total_rows = (GRID_TOTAL + cols - 1) // cols if items else 0
        # heading (size 24 -> 30px) + column gap precede the first row
        self._grid_geom = (grid_top + 30 + gap, pitch, cols, total_rows)
        start, end = self.grid_window
        start = max(0, min(start, total_rows))
        end = max(start, min(end, total_rows))
        rows = []
        if start > 0:
            rows.append(Spacer(h=start * pitch - gap))
        for r in range(start, end):
            entries = []
            for c in range(cols):
                v = r * cols + c
                if v >= GRID_TOTAL:
                    break
                it = dict(items[v % len(items)])
                entries.append(dict(it, vidx=v))
            if entries:
                rows.append(self._grid_image_map(entries))
        if end < total_rows:
            rows.append(Spacer(h=(total_rows - end) * pitch - gap))
        return Column(
            [Text(heading, size=24, bold=True)] + rows, gap=gap
        )

    def _grid_image_map(self, entries):
        s = self.strips.strip(entries)
        regions = [
            dict(
                r,
                id="grid-tile-%d" % e["vidx"],
                on_click=lambda i=e["idx"]: self._pick(i),
                on_context=lambda x, y, i=e["idx"]: self._open_menu(
                    i, x, y
                ),
            )
            for r, e in zip(s["regions"], entries)
        ]
        return ImageMap(s["src"], s["iw"], s["ih"], regions=regions)

    def _on_page_scroll(self, offset, maximum):
        if not self._grid_geom:
            return
        grid_top, pitch, cols, total_rows = self._grid_geom
        # viewport top row within the grid (page viewport starts at 76)
        row = int((offset - grid_top) // pitch)
        start = max(0, row - WINDOW_BEHIND)
        end = min(total_rows, max(row, 0) + WINDOW_AHEAD)
        cur = self.grid_window
        if abs(start - cur[0]) >= 2 or abs(end - cur[1]) >= 2:
            log.info(
                "grid window: rows %s -> (%d, %d) at offset %d",
                cur,
                start,
                end,
                offset,
            )
            self.grid_window = (start, end)
            self.app.invalidate()

    # ------------------------------------------------------ test pages

    def _tab(self, label, page):
        active = self.page == page
        return Button(
            label,
            id="tab-" + page,
            bg="7aa2f7" if active else "2a2a2a",
            fg="101010" if active else "eeeeee",
            on_click=lambda p=page: self._set_page(p),
        )

    def _progress_widget(self, frac, w=260, h=10):
        return Progress(frac, w=w, h=h)

    def _widgets_page(self, w):
        checks = Column(
            [
                Checkbox(
                    key.capitalize(),
                    self.opts[key],
                    id="chk-" + key,
                    on_toggle=lambda k=key: self._toggle(k),
                )
                for key in ("autoplay", "transcode", "subtitles")
            ],
            gap=12,
        )
        sliders = Column(
            [
                Row(
                    [
                        Text("Volume", size=18, w=80),
                        Slider(
                            "vol",
                            value=self.volume,
                            on_change=self._on_volume,
                            w=220,
                        ),
                        Text("%d%%" % self.volume, size=18, w=60),
                    ],
                    gap=10,
                    align="center",
                ),
                Row(
                    [
                        Text("Busy", size=18, w=80),
                        Busy(),
                        Spacer(w=30),
                        Text("Progress", size=18),
                        self._progress_widget(self.progress / 100.0),
                    ],
                    gap=10,
                    align="center",
                ),
            ],
            gap=16,
        )
        entries = Row(
            [
                TextBox(
                    "pw",
                    text=self.password,
                    placeholder="Password…",
                    mask=True,
                    w=220,
                    on_change=lambda v: setattr(self, "password", v),
                ),
                TextBox(
                    "seltb",
                    text=self.sel_text,
                    w=320,
                    on_change=lambda v: setattr(self, "sel_text", v),
                ),
                Button(
                    "Show Dialog",
                    id="btn-dialog",
                    on_click=lambda: (
                        setattr(self, "dialog_open", True),
                        self.app.invalidate(),
                    ),
                ),
                Button("Show Toast", id="btn-toast",
                       on_click=self._show_toast),
                Button(
                    "Hold Me (%d)" % self.hold_count,
                    id="btn-hold",
                    repeat=True,
                    tip="Auto-repeats while held",
                    on_click=self._hold_tick,
                ),
            ],
            gap=12,
            align="center",
        )
        icons_row = Row(
            [
                Box(  # icon+label button
                    [Icon("play_arrow", 22), Text("Play", size=18)],
                    id="btn-play",
                    direction="row",
                    gap=6,
                    pad=8,
                    bg="333333",
                    hover={"fill": "4a4a4a"},
                    radius=6,
                    align="center",
                    on_click=lambda: self._pick(0),
                ),
                Box(  # icon-only button
                    [Icon("skip_next", 22)],
                    id="btn-next",
                    pad=8,
                    bg="333333",
                    hover={"fill": "4a4a4a"},
                    radius=6,
                    on_click=lambda: self._pick(1),
                ),
                Row(  # label with tinted icon
                    [
                        Icon("favorite", 18, color="e05070"),
                        Text("Favorites", size=18),
                    ],
                    gap=6,
                    align="center",
                ),
                Dropdown(
                    "dtype",
                    ["Movies", "Music", "Radio"],
                    icons=["movie", "queue_music", "radio"],
                    w=170,
                    on_select=lambda i, v: setattr(self, "dtype", v),
                ),
            ],
            gap=14,
            align="center",
        )
        table = Column(
            [
                Table(
                    columns=[
                        {"label": "#", "w": 50},
                        {"label": "Title", "w": 320},
                        {"label": "Year", "w": 80},
                    ],
                    rows=[
                        {
                            "id": "trow-%d" % i,
                            "cells": [str(r["num"]), r["title"],
                                      str(r["year"])],
                            "selected": i in self.table_multi
                            or self.table_sel == i,
                            # required first param -> receives the click
                            # modifier dict (shift-range / ctrl-toggle)
                            "on_click": lambda m, i=i: self._tbl_select(
                                i, m
                            ),
                            "on_dbl": lambda i=i: self._tbl_activate(i),
                        }
                        for i, r in enumerate(self.table_rows)
                    ],
                    size=17,
                    row_h=32,
                    selected_bg="335a9e",
                    hover_bg="2e2e2e",
                    w=480,
                ),
                Row(
                    [
                        Button("Move Up", id="tbl-up",
                               on_click=lambda: self._tbl_move(-1)),
                        Button("Move Down", id="tbl-down",
                               on_click=lambda: self._tbl_move(1)),
                    ],
                    gap=10,
                ),
            ],
            gap=4,
        )
        page = Column(
            [
                Text("Widget gallery", size=24, bold=True),
                Row([checks, Spacer(w=60), sliders], gap=10),
                icons_row,
                entries,
                Text("Track table (click / shift-range / ctrl-toggle)",
                     size=18, bold=True),
                table,
                Text(
                    "Wrapped text: " + " ".join(
                        "the quick brown fox jumps over the lazy dog"
                        .split() * 4
                    ),
                    id="wraptxt",
                    size=16,
                    color="c8c8c8",
                    wrap=True,
                    max_lines=3,
                    w=420,
                ),
            ],
            pad=16,
            gap=18,
        )
        return VScroll(page, id="wpage", flex=1)

    def _logs_page(self, w):
        lines = Column(
            [
                Text(line, size=15, color="c8c8c8")
                for line in self.log_lines
            ],
            pad=16,
            gap=2,
        )
        return VScroll(lines, id="logs", flex=1)

    def build(self, size):
        w, h = size
        items = self._filtered()
        header = Row(
            [
                Text("mpvtk demo", size=28, bold=True),
                Spacer(),
                TextBox(
                    "search",
                    text=self.query,
                    placeholder="Search…",
                    w=260,
                    on_change=self._on_query,
                    on_submit=self._on_query,
                ),
                Dropdown(
                    "sort",
                    ["Name", "Year", "Shuffled"],
                    selected=["Name", "Year", "Shuffled"].index(self.sort),
                    w=140,
                    on_select=self._on_sort,
                ),
                Button(
                    "Clear",
                    id="clear",
                    on_click=lambda: self._on_query(""),
                ),
            ],
            pad=16,
            gap=12,
            align="center",
            h=76,
        )
        tabs = Row(
            [
                self._tab("Browse", "browse"),
                self._tab("Widgets", "widgets"),
                self._tab("Logs", "logs"),
            ],
            pad=10,
            gap=8,
            h=56,
        )
        if self.page == "widgets":
            content = self._widgets_page(w)
        elif self.page == "logs":
            content = self._logs_page(w)
        else:
            # content-space y where the grid section starts (windowing):
            # page pad + two sections (heading 30 + gap 8 + scroll) + gaps
            sec_h = 30 + 8 + STRIP_H + 6
            grid_top = 16 + 2 * (sec_h + 20)
            page = Column(
                [
                    self._row_section(
                        "Continue Watching", items[:12], "row-cw"
                    ),
                    self._row_section("Movies", items, "row-mv"),
                    self._grid_section("All Media", items, w, grid_top),
                ],
                pad=16,
                gap=20,
            )
            content = VScroll(
                page,
                id="page",
                flex=1,
                on_scroll=self._on_page_scroll,
            )
        footer = Row(
            [Text(self.status, id="status", size=18, color="aaaaaa")],
            pad=12,
            h=44,
            bg="1d1d1d",
        )
        children = [header, tabs, content, footer]
        if self.menu:
            children.append(
                Menu(
                    "ctxmenu",
                    ["Play", "Mark Watched", "Toggle Favorite"],
                    self.menu["x"],
                    self.menu["y"],
                    icons=["play_arrow", "edit", "favorite"],
                    on_select=self._menu_action,
                    on_dismiss=self._close_menu,
                )
            )
        if self.dialog_open:
            children.append(
                Dialog(
                    "dlg",
                    Column(
                        [
                            Text("Confirm action?", size=22, bold=True),
                            Text(
                                "Modal test: grabs input, ESC or "
                                "click-away dismisses.",
                                size=16,
                                color="aaaaaa",
                            ),
                            Row(
                                [
                                    Spacer(),
                                    Button("Cancel", id="dlg-cancel",
                                           on_click=self._dlg_close),
                                    Button("Confirm", id="dlg-ok",
                                           on_click=self._dlg_confirm),
                                ],
                                gap=10,
                            ),
                        ],
                        pad=24,
                        gap=14,
                        bg="1e1e1e",
                        radius=12,
                        border="555555",
                        w=420,
                    ),
                    on_dismiss=self._dlg_close,
                )
            )
        if self.toast:
            children.append(
                Float(
                    Box(
                        [Text(self.toast, size=17)],
                        pad=14,
                        bg="2d3f2d",
                        radius=10,
                        border="4a7a4a",
                        direction="row",
                    ),
                    x=w - 420,
                    y=h - 110,
                )
            )
        return Column(
            children,
            w=w,
            h=h,
            align="stretch",
        )

    def run(self):
        threading.Thread(
            target=self._progress_animator, daemon=True
        ).start()
        self.app.run(self.build)


# ------------------------------------------------------------- selftest


def _selftest(demo, outdir):
    import shutil
    import subprocess

    app = demo.app
    os.makedirs(outdir, exist_ok=True)
    results = []

    def shot(name):
        time.sleep(0.35)
        path = os.path.join(outdir, name + ".png")
        try:
            app.screenshot(path)
        except Exception:
            # No video track -> mpv can't screenshot; grab the X root.
            if shutil.which("import"):
                subprocess.run(
                    ["import", "-window", "root", path],
                    check=False,
                    timeout=15,
                )
            else:
                results.append("screenshot %s SKIPPED (no tool)" % name)
        time.sleep(0.15)

    def check(name, cond, detail=""):
        results.append(
            "%s %s %s" % ("PASS" if cond else "FAIL", name, detail)
        )

    if not app.ready.wait(15):
        results.append("FAIL renderer never became ready")
        app.quit()
        return results
    time.sleep(0.6)
    shot("01-initial")
    st = app.debug_state()
    check("ready-size", st and st.get("w", 0) > 0, str(st and st.get("w")))
    check(
        "measured-metrics",
        st and st.get("has_metrics"),
        "font=%s" % (st and st.get("font")),
    )
    if app.in_process:
        srcs = [e["src"] for e in demo.strips._cache.values()]
        check(
            "memory-overlays",
            srcs and all(s.startswith("&") for s in srcs),
            "%d in-memory strips" % len(srcs),
        )
    novl = (st or {}).get("overlays", 0)
    check(
        "strip-overlay-budget",
        2 <= novl <= 12,
        "overlays=%s (strips, not tiles)" % novl,
    )

    # first tile in sort order is guaranteed inside the row viewport
    first_tile = "row-cw-tile-%d" % demo._filtered()[0]["idx"]
    app.debug(cmd="hover", id=first_tile)
    shot("02-hover")
    st = app.debug_state()
    check(
        "hover-id",
        st and st.get("hover") == first_tile,
        str(st and st.get("hover")),
    )

    app.debug(cmd="click", id=first_tile)
    shot("03-clicked")
    check("click-status", "Selected:" in demo.status, demo.status)

    app.debug(cmd="wheel", id="page", dir=1, steps=4, axis="y")
    shot("04-vscroll")
    st = app.debug_state()
    scr = (st or {}).get("scroll") or {}
    check("vscroll-offset", scr.get("page", 0) > 0, str(scr))
    # the property mirror lets Python read offsets synchronously
    sync = app.scroll_offsets()
    check(
        "scroll-property-sync",
        abs(sync.get("page", -1) - scr.get("page", 0)) < 1,
        str(sync),
    )

    app.debug(cmd="wheel", id="row-mv", dir=1, steps=3, axis="x")
    shot("05-hscroll")
    st = app.debug_state()
    scr = (st or {}).get("scroll") or {}
    check("hscroll-offset", scr.get("row-mv", 0) > 0, str(scr))

    # deep-scroll into the virtualized grid; the debounced scroll event
    # should move the materialization window
    app.debug(cmd="wheel", id="page", dir=1, steps=40, axis="y")
    time.sleep(0.8)
    shot("05b-deep-grid")
    check(
        "grid-window-moved",
        demo.grid_window[0] > 0,
        str(demo.grid_window),
    )
    st = app.debug_state()
    novl = (st or {}).get("overlays", 0)
    check("deep-grid-overlays", 1 <= novl <= 12, "overlays=%s" % novl)
    scr = (st or {}).get("scroll") or {}
    check("deep-grid-offset", scr.get("page", 0) > 2000, str(scr))

    app.debug(cmd="wheel", id="page", dir=-1, steps=80)  # back to top
    time.sleep(0.8)
    app.debug(cmd="click", id="sort")
    shot("06-dropdown-open")
    st = app.debug_state()
    check("dropdown-open", st and st.get("dd_open") == "sort")
    app.debug(cmd="popup", index=1)  # select "Year"
    shot("06b-dropdown-selected")
    time.sleep(0.2)
    check("dropdown-select", demo.sort == "Year", demo.sort)
    st = app.debug_state()
    check("dropdown-closed", st and not st.get("dd_open"))

    # context menu: open on right-click, act, and dismiss
    ctx_item = demo._filtered()[0]
    ctx_tile = "row-cw-tile-%d" % ctx_item["idx"]
    app.debug(cmd="rclick", id=ctx_tile)
    time.sleep(0.4)
    shot("06c-context-open")
    st = app.debug_state()
    check(
        "context-open",
        st and st.get("menu_open") and demo.menu is not None,
        str(demo.menu),
    )
    app.debug(cmd="menu", index=1)  # "Mark Watched"
    time.sleep(0.5)
    shot("06d-context-watched")
    check(
        "context-action",
        demo.items[ctx_item["idx"]]["watched"],
        demo.status,
    )
    check("context-closed", demo.menu is None)
    app.debug(cmd="rclick", id=ctx_tile)
    time.sleep(0.4)
    app.debug(cmd="click", x=600, y=740)  # click away (footer)
    time.sleep(0.4)
    check("context-dismiss", demo.menu is None)

    app.debug(cmd="click", id="search")
    app.debug(cmd="text", s="ze")
    shot("07-filter")
    check("filter-query", demo.query == "ze", demo.query)
    check(
        "filter-narrowed",
        len(demo._filtered()) < len(demo.items),
        "%d items" % len(demo._filtered()),
    )
    st = app.debug_state()
    check("focus", st and st.get("focus") == "search")

    app.debug(cmd="key", name="BS")
    app.debug(cmd="key", name="BS")
    shot("08-cleared")
    check("bs-restores", demo.query == "", repr(demo.query))

    # ---- widget-gallery page ----
    app.debug(cmd="click", id="tab-widgets")
    time.sleep(0.4)
    shot("09-widgets")
    from .layout import layout as _layout

    nodes, _ = _layout(demo.build((1280, 720)), 1280, 720)
    n_icons = sum(1 for n in nodes if n["t"] == "icon")
    dd_icons = any(n.get("icons") for n in nodes if n["t"] == "dropdown")
    check("vector-icons", n_icons >= 3 and dd_icons,
          "%d icon nodes" % n_icons)
    app.debug(cmd="click", id="dtype")
    time.sleep(0.4)
    shot("09b-icon-dropdown")
    app.debug(cmd="popup", index=1)
    time.sleep(0.3)
    check("icon-dropdown-select", getattr(demo, "dtype", None) == "Music")
    before = demo.opts["transcode"]
    app.debug(cmd="click", id="chk-transcode")
    time.sleep(0.3)
    check("checkbox-toggle", demo.opts["transcode"] != before)

    app.debug(cmd="click", id="vol")  # center click ~= 50%
    time.sleep(0.4)
    check("slider-click", 40 <= demo.volume <= 60, str(demo.volume))

    p0 = demo.progress
    time.sleep(0.5)
    check(
        "progress-animates",  # background thread + invalidate wake
        demo.progress != p0,
        "%.1f -> %.1f" % (p0, demo.progress),
    )

    app.debug(cmd="click", id="pw")
    app.debug(cmd="text", s="hunter2")
    time.sleep(0.3)
    shot("10-password")
    check("password-value", demo.password == "hunter2", demo.password)

    app.debug(cmd="click", id="seltb")
    app.debug(cmd="key", name="CTRLA")
    app.debug(cmd="text", s="replaced")
    time.sleep(0.3)
    check("selection-replace", demo.sel_text == "replaced",
          demo.sel_text)

    app.debug(cmd="click", id="btn-dialog")
    time.sleep(0.4)
    shot("11-dialog")
    st = app.debug_state()
    check(
        "dialog-open",
        st and st.get("modal_open") and demo.dialog_open,
    )
    app.debug(cmd="click", x=40, y=300)  # click-away dismiss
    time.sleep(0.4)
    check("dialog-dismiss", not demo.dialog_open)
    app.debug(cmd="click", id="btn-dialog")
    time.sleep(0.4)
    app.debug(cmd="click", id="dlg-ok")
    time.sleep(0.4)
    check(
        "dialog-confirm",
        "Confirmed" in demo.status and not demo.dialog_open,
        demo.status,
    )

    app.debug(cmd="click", id="btn-toast")
    time.sleep(0.4)
    shot("12-toast")
    check("toast-shown", demo.toast is not None)
    time.sleep(2.8)
    check("toast-auto-dismiss", demo.toast is None)

    app.debug(cmd="click", id="trow-3")
    time.sleep(0.3)
    check("table-select", demo.table_sel == 3, str(demo.table_sel))
    # click modifiers: shift extends from the anchor, ctrl toggles
    app.debug(cmd="click", id="trow-1", shift=True)
    time.sleep(0.3)
    check(
        "table-shift-range",
        demo.table_multi == {1, 2, 3},
        str(sorted(demo.table_multi)),
    )
    app.debug(cmd="click", id="trow-5", ctrl=True)
    time.sleep(0.3)
    check(
        "table-ctrl-toggle",
        demo.table_multi == {1, 2, 3, 5},
        str(sorted(demo.table_multi)),
    )
    app.debug(cmd="click", id="trow-3")
    time.sleep(0.3)
    check("table-plain-resets", demo.table_multi == {3},
          str(sorted(demo.table_multi)))
    moved_title = demo.table_rows[3]["title"]
    # the reorder buttons sit below the fold: scroll them into view
    app.debug(cmd="wheel", id="wpage", dir=1, steps=6, axis="y")
    time.sleep(0.3)
    app.debug(cmd="click", id="tbl-up")
    time.sleep(0.3)
    check(
        "table-reorder",
        demo.table_rows[2]["title"] == moved_title
        and demo.table_sel == 2,
    )

    # hold-repeat: press fires immediately, refires while held, and
    # the release adds nothing
    app.debug(cmd="wheel", id="wpage", dir=-1, steps=10, axis="y")
    time.sleep(0.3)
    demo.hold_count = 0
    app.debug(cmd="down", id="btn-hold")
    time.sleep(1.0)
    app.debug(cmd="up", id="btn-hold")
    time.sleep(0.3)
    held = demo.hold_count
    check("hold-repeat-fires", held >= 3, "%d clicks" % held)
    time.sleep(0.4)
    check("hold-repeat-stops", demo.hold_count == held,
          "%d after release" % demo.hold_count)

    # double-click on a table row activates it (dbl event)
    app.debug(cmd="dbl", id="trow-2")
    time.sleep(0.4)
    check("table-dblclick", "Activated" in demo.status, demo.status)

    # tooltip appears after the hover delay and clears on hover-away
    app.debug(cmd="hover", id="btn-hold")
    time.sleep(0.9)
    st = app.debug_state()
    check(
        "tooltip-shows",
        (st or {}).get("tip") == "Auto-repeats while held",
        str((st or {}).get("tip")),
    )
    shot("12b-tooltip")
    app.debug(cmd="hover", id="btn-toast")
    time.sleep(0.3)
    st = app.debug_state()
    check("tooltip-clears", not (st or {}).get("tip"))

    # ---- spatial navigation (10ft) ----
    app.debug(cmd="nav", dir="down")  # first press focuses something
    time.sleep(0.3)
    st = app.debug_state()
    check("nav-first-focus", bool((st or {}).get("nav")),
          str((st or {}).get("nav")))
    app.debug(cmd="nav", id="btn-toast")
    app.debug(cmd="nav", dir="left")
    time.sleep(0.3)
    st = app.debug_state()
    check("nav-left-moves", (st or {}).get("nav") == "btn-dialog",
          str((st or {}).get("nav")))
    app.debug(cmd="nav", id="btn-toast")
    app.debug(cmd="nav", action="enter")
    time.sleep(0.4)
    check("nav-enter-clicks", demo.toast is not None)
    time.sleep(2.6)  # let the toast auto-dismiss before moving on
    # focusing an off-viewport node scrolls it into view
    app.debug(cmd="wheel", id="wpage", dir=-1, steps=12, axis="y")
    time.sleep(0.3)
    app.debug(cmd="nav", id="tbl-up")
    time.sleep(0.3)
    st = app.debug_state()
    scr = (st or {}).get("scroll") or {}
    check("nav-scroll-into-view", scr.get("wpage", 0) > 0,
          str(scr.get("wpage")))

    # wrapped text emits one node per line, stacked a line apart
    wnodes, _ = _layout(demo.build((1280, 720)), 1280, 720)
    wl = [n for n in wnodes
          if n["id"] == "wraptxt" or n["id"].startswith("wraptxt.l")]
    check("text-wrap-lines", len(wl) == 3, "%d lines" % len(wl))
    check(
        "text-wrap-stacked",
        len(wl) == 3
        and wl[0]["x"] == wl[1]["x"]
        and wl[1]["y"] > wl[0]["y"]
        and wl[-1]["text"].endswith("…"),
        "%r" % [n["text"][:16] for n in wl],
    )

    app.debug(cmd="click", id="tab-logs")
    time.sleep(0.4)
    app.debug(cmd="wheel", id="logs", dir=1, steps=5, axis="y")
    time.sleep(0.3)
    shot("13-logs")
    st = app.debug_state()
    scr = (st or {}).get("scroll") or {}
    check("logs-scroll", scr.get("logs", 0) > 0, str(scr.get("logs")))

    # ---- textbox: drag-selection + built-in context menu ----
    app.debug(cmd="click", id="tab-widgets")
    time.sleep(0.4)
    app.debug(cmd="click", id="seltb")
    app.debug(cmd="key", name="CTRLA")
    app.debug(cmd="text", s="The quick fox")
    time.sleep(0.3)
    app.debug(cmd="tbdrag", id="seltb", a=4, b=9)  # select "quick"
    time.sleep(0.3)
    shot("14-drag-select")
    app.debug(cmd="text", s="lazy")
    time.sleep(0.3)
    check(
        "drag-select-replace",
        demo.sel_text == "The lazy fox",
        demo.sel_text,
    )

    app.debug(cmd="rclick", id="seltb")
    time.sleep(0.4)
    shot("15-tbmenu")
    st = app.debug_state()
    check("tbmenu-open", st and st.get("tb_menu"))
    app.debug(cmd="tbmenu", index=3)  # Select All
    time.sleep(0.3)
    app.debug(cmd="rclick", id="seltb")
    time.sleep(0.3)
    app.debug(cmd="tbmenu", index=0)  # Cut (deletes selection)
    time.sleep(0.3)
    check("tbmenu-cut", demo.sel_text == "", repr(demo.sel_text))

    # ctrl word-ops + ctrl+x
    app.debug(cmd="click", id="seltb")
    app.debug(cmd="text", s="alpha beta gamma")
    app.debug(cmd="key", name="CLEFT")  # to start of "gamma"
    app.debug(cmd="key", name="CBS")  # delete "beta "
    time.sleep(0.3)
    check("word-nav-delete", demo.sel_text == "alpha gamma",
          demo.sel_text)
    app.debug(cmd="key", name="CSRIGHT")  # select "gamma"
    app.debug(cmd="key", name="CUT")  # ctrl+x path
    time.sleep(0.3)
    check("ctrlx-cut", demo.sel_text == "alpha ", repr(demo.sel_text))

    # double-click word select / triple-click select all
    app.debug(cmd="click", id="seltb")
    app.debug(cmd="key", name="CTRLA")
    app.debug(cmd="text", s="alpha beta gamma")
    app.debug(cmd="dbl", id="seltb", at=8)  # inside "beta"
    time.sleep(0.3)
    app.debug(cmd="text", s="X")
    time.sleep(0.3)
    check(
        "dbl-word-select",
        demo.sel_text == "alpha X gamma",
        demo.sel_text,
    )
    app.debug(cmd="triple", id="seltb", at=3)
    time.sleep(0.3)
    app.debug(cmd="text", s="reset")
    time.sleep(0.3)
    check("triple-select-all", demo.sel_text == "reset", demo.sel_text)

    # unicode: text arrives via any_unicode; editing is codepoint-safe
    app.debug(cmd="key", name="CTRLA")
    app.debug(cmd="text", s="café жизнь")
    time.sleep(0.3)
    check("unicode-input", demo.sel_text == "café жизнь", demo.sel_text)
    from .layout import _measured

    check(
        "dynamic-metrics",
        _measured is not None and "ж" in _measured,
        "measured %s chars" % (len(_measured or ())),
    )
    for _ in range(6):  # delete "ь", "н", "з", "и", "ж", space
        app.debug(cmd="key", name="BS")
    time.sleep(0.3)
    check("utf8-backspace", demo.sel_text == "café", repr(demo.sel_text))
    # real input stack: keypress goes through mpv input -> any_unicode
    app.backend.command("keypress", "日")
    app.backend.command("keypress", "本")
    time.sleep(0.4)
    check(
        "any-unicode-keypress",
        demo.sel_text == "café日本",
        repr(demo.sel_text),
    )
    app.debug(cmd="key", name="BS")
    app.debug(cmd="key", name="BS")
    app.debug(cmd="key", name="BS")
    time.sleep(0.3)
    check("utf8-mixed-edit", demo.sel_text == "caf", repr(demo.sel_text))

    # ---- Stack z-order + occlusion over the browse strip ----
    app.debug(cmd="click", id="tab-browse")
    time.sleep(0.6)
    shot("16-stack")
    st = app.debug_state()
    ov = (st or {}).get("ov") or {}
    strip_slots = [s for k, s in ov.items()
                   if k.startswith("row-cw-img#")]
    badge_slot = ov.get("zbadge#1")
    check(
        "stack-bitmap-above",
        badge_slot is not None
        and strip_slots
        and badge_slot > max(strip_slots),
        "badge=%s strip=%s" % (badge_slot, strip_slots),
    )
    check(
        "stack-occlude-splits",
        len(strip_slots) >= 2,
        "%d strip pieces (occlude punch)" % len(strip_slots),
    )

    # nav paging: arrow-right past the viewport edge must auto-scroll
    # the carousel to reach fully clipped tiles
    ftile = "row-cw-tile-%d" % demo._filtered()[0]["idx"]
    app.debug(cmd="nav", id=ftile)
    for _ in range(12):
        app.debug(cmd="nav", dir="right")
    time.sleep(0.5)
    st = app.debug_state()
    scr = (st or {}).get("scroll") or {}
    check("nav-carousel-autoscroll", scr.get("row-cw", 0) > 0,
          str(scr.get("row-cw")))

    app.quit()
    return results


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="mpvtk demo")
    parser.add_argument("--backend", default="jsonipc",
                        choices=["jsonipc", "libmpv"])
    parser.add_argument("--selftest", metavar="OUTDIR", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    demo = Demo(backend=args.backend)
    results = []
    if args.selftest:
        t = threading.Thread(
            target=lambda: results.extend(_selftest(demo, args.selftest)),
            daemon=True,
        )
        t.start()
    demo.run()
    if args.selftest:
        t.join(timeout=5)
        print("\n".join(results))
        return 1 if any(r.startswith("FAIL") for r in results) else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
