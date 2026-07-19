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
import tempfile
import threading
import time

from .app import MpvtkApp
from .rawimage import write_bgra
from .widgets import (
    Button,
    Column,
    Dropdown,
    HScroll,
    ImageMap,
    Menu,
    Row,
    Spacer,
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
    """

    def __init__(self, cache_dir):
        self.dir = cache_dir
        self.fonts = _fonts()
        self._cache = {}
        self._counter = 0

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
            return hit
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
        self._counter += 1
        src = os.path.join(self.dir, "strip%d.bgra" % self._counter)
        write_bgra(img, src)
        entry = {"src": src, "iw": iw, "ih": STRIP_H, "regions": regions}
        self._cache[key] = entry
        return entry


class Demo:
    def __init__(self, backend="jsonipc"):
        self.cache = tempfile.mkdtemp(prefix="mpvtk-demo-")
        self.items = make_library(40)
        self.strips = StripStore(self.cache)
        self.query = ""
        self.sort = "Name"
        self.status = "Hover and click tiles; scroll rows and the page."
        self.grid_window = (0, WINDOW_AHEAD + 4)  # materialized row range
        self._grid_geom = None  # (grid_top, pitch, cols, total_rows)
        self.menu = None  # {"idx": item, "x":, "y":} while a menu is open
        self.app = MpvtkApp(backend=backend)

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
        return ImageMap(s["src"], s["iw"], s["ih"], regions=regions)

    def _row_section(self, heading, items, row_id):
        return Column(
            [
                Text(heading, size=24, bold=True),
                HScroll(
                    self._image_map(items, row_id),
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
            self.grid_window = (start, end)
            self.app.invalidate()

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
        # content-space y where the grid section starts (for windowing):
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
        footer = Row(
            [Text(self.status, id="status", size=18, color="aaaaaa")],
            pad=12,
            h=44,
            bg="1d1d1d",
        )
        children = [
            header,
            VScroll(
                page,
                id="page",
                flex=1,
                on_scroll=self._on_page_scroll,
            ),
            footer,
        ]
        if self.menu:
            children.append(
                Menu(
                    "ctxmenu",
                    ["Play", "Mark Watched", "Toggle Favorite"],
                    self.menu["x"],
                    self.menu["y"],
                    on_select=self._menu_action,
                    on_dismiss=self._close_menu,
                )
            )
        return Column(
            children,
            w=w,
            h=h,
            align="stretch",
        )

    def run(self):
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
