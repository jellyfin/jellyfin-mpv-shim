"""mpvtk demo: exercises tiles, h/v scrolling, scrollbar, textbox and
dropdown against generated placeholder posters.

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
    Box,
    Button,
    Column,
    Dropdown,
    HScroll,
    Image,
    Row,
    Spacer,
    Text,
    TextBox,
    VScroll,
)

log = logging.getLogger("mpvtk.demo")

TILE_W, TILE_H = 140, 200

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


def make_posters(cache_dir, n):
    """Generate n placeholder posters; returns [{src, title, year}]."""
    from PIL import Image as PILImage, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 44)
        small = ImageFont.truetype("DejaVuSans.ttf", 16)
    except OSError:
        font = small = ImageFont.load_default()

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
            font=font,
            anchor="mm",
            fill=(240, 240, 240),
        )
        dr.text(
            (TILE_W // 2, TILE_H - 24),
            title.split()[0],
            font=small,
            anchor="mm",
            fill=(220, 220, 220),
        )
        dr.rectangle(
            [0, 0, TILE_W - 1, TILE_H - 1], outline=(20, 20, 20)
        )
        src = os.path.join(cache_dir, "poster%d.bgra" % i)
        write_bgra(img, src)
        items.append(
            {"src": src, "title": title, "year": 1980 + rnd.randrange(45)}
        )
    return items


class Demo:
    def __init__(self, backend="jsonipc"):
        self.cache = tempfile.mkdtemp(prefix="mpvtk-demo-")
        self.items = make_posters(self.cache, 40)
        self.query = ""
        self.sort = "Name"
        self.status = "Hover and click tiles; scroll rows and the page."
        self.app = MpvtkApp(backend=backend)

    # ------------------------------------------------------------- state

    def _filtered(self):
        items = [
            (i, it)
            for i, it in enumerate(self.items)
            if self.query.lower() in it["title"].lower()
        ]
        if self.sort == "Name":
            items.sort(key=lambda p: p[1]["title"])
        elif self.sort == "Year":
            items.sort(key=lambda p: p[1]["year"])
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

    # ------------------------------------------------------------- build

    def _tile(self, idx, item, section):
        return Column(
            [
                Image(
                    item["src"],
                    TILE_W,
                    TILE_H,
                    id="%s-tile-%d" % (section, idx),
                    on_click=lambda i=idx: self._pick(i),
                    hover={"bc": "7aa2f7", "bw": 3},
                ),
                Text(
                    item["title"],
                    size=16,
                    color="cccccc",
                    w=TILE_W,
                ),
                Text(
                    str(item["year"]),
                    size=14,
                    color="888888",
                    w=TILE_W,
                ),
            ],
            gap=4,
            w=TILE_W,
        )

    def _row_section(self, heading, items, row_id):
        tiles = Row(
            [self._tile(i, it, row_id) for i, it in items],
            gap=14,
            pad=2,
        )
        return Column(
            [
                Text(heading, size=24, bold=True),
                HScroll(tiles, id=row_id, h=TILE_H + 52),
            ],
            gap=8,
        )

    def _grid_section(self, heading, items, width):
        cols = max(1, int((width - 48 + 14) // (TILE_W + 14)))
        rows = [
            Row(
                [
                    self._tile(i, it, "grid")
                    for i, it in items[r : r + cols]
                ],
                gap=14,
            )
            for r in range(0, len(items), cols)
        ]
        return Column([Text(heading, size=24, bold=True)] + rows, gap=12)

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
        page = Column(
            [
                self._row_section(
                    "Continue Watching", items[:12], "row-cw"
                ),
                self._row_section("Movies", items, "row-mv"),
                self._grid_section("All Media", items, w),
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
        return Column(
            [
                header,
                VScroll(page, id="page", flex=1),
                footer,
            ],
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
        "overlays-visible",
        st and st.get("overlays", 0) > 5,
        "overlays=%s" % (st and st.get("overlays")),
    )

    # first tile in sort order is guaranteed inside the row viewport
    first_tile = "row-cw-tile-%d" % demo._filtered()[0][0]
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

    app.debug(cmd="wheel", id="page", dir=-1, steps=20)  # back to top
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
