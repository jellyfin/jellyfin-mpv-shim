"""What a hover costs in overlay traffic, against a real mpv.

    xvfb-run -a python3 tools/probe_hover_overlay_cost.py          # Linux
    winrun probe "%JMSPY% tools\\probe_hover_overlay_cost.py"      # Windows

A tile row is ONE bitmap (strips.py), handed to mpv with `overlay-add`. Moving
the pointer onto a tile changes a chip the size of a coin, so a repaint should
re-issue the chip and nothing else -- the renderer's overlay slots are sticky
for exactly that reason. This prints what actually goes over per pointer move,
counted by the renderer itself (`debug_state`'s ov_adds / ov_bytes).

`--legacy` reproduces the behaviour before the row bitmaps were given names of
their own: the Stack that floats the chip renamed the row under it, so the
renderer saw a bitmap depart and an unrelated one arrive. Run both and compare.

Not a test: it needs a real mpv and a window, and what it produces is a number
to read rather than a threshold to assert.
"""

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "integration"))
import _harness as h  # noqa: E402

h.prime_args()


def _source():
    """Enough rows and tiles for a grid: the shape a pointer crosses."""
    class FakeSource:
        libraries = [{"Id": "lib1", "Name": "Movies",
                      "Type": "CollectionFolder",
                      "CollectionType": "movies"}]

        def servers(self):
            return [{"uuid": "srv1", "name": "Test"}]

        def get_libraries(self, server_uuid):
            return list(self.libraries)

        def get_home_prefs(self, server_uuid, refresh=False):
            from jellyfin_mpv_shim.mpvtk_browser import home_sections
            return list(home_sections.DEFAULT_LAYOUT), frozenset()

        def get_home_rows(self, server_uuid, libraries=None, sections=None,
                          layout=None, latest_excludes=None):
            return []

        def get_library_items(self, server_uuid, parent_id, start_index=0,
                              **kw):
            items = [{"Id": "g%d" % i, "Name": "Grid %d" % i,
                      "Type": "Movie", "PrimaryImageAspectRatio": 2 / 3}
                     for i in range(48)]
            return items[start_index:start_index + 48], len(items)

        def image_spec(self, item, image_type="Primary", width=280,
                       inherit=True):
            return None

        def image_url(self, *a, **k):
            return None

    return FakeSource()


def _spawn(geometry):
    from jellyfin_mpv_shim.mpvtk.app import _SPAWN_OPTS

    if h.BACKEND == "jsonipc":
        import python_mpv_jsonipc
        opts = dict(_SPAWN_OPTS)
        opts["geometry"] = geometry
        return python_mpv_jsonipc.MPV(start_mpv=True, **opts), True
    import mpv as libmpv
    opts = {k.replace("_", "-"): v for k, v in _SPAWN_OPTS.items()}
    opts["geometry"] = geometry
    return libmpv.MPV(**opts), False


def _use_legacy_ids():
    """Take the row bitmap's name away again, which is what put it back on
    its path in the tree -- and therefore renamed it whenever the chip
    wrapped it in a Stack."""
    from jellyfin_mpv_shim.mpvtk_browser import tile_renderer

    base = tile_renderer.ImageMap

    class Unnamed(base):
        def __init__(self, *a, **kw):
            kw.pop("id", None)
            super().__init__(*a, **kw)

    tile_renderer.ImageMap = Unnamed


def _settled(app, timeout=4.0):
    """debug_state once the renderer has stopped issuing overlays."""
    last = None
    end = time.time() + timeout
    while time.time() < end:
        st = app.debug_state() or {}
        key = (st.get("ov_adds"), st.get("overlays"))
        if last is not None and key == last and key[0] is not None:
            return st
        last = key
        time.sleep(0.12)
    return app.debug_state() or {}


def _wait_for_chip(app, tile_id, timeout=4.0):
    """debug_state once the chip for ``tile_id`` is on screen.

    Waiting for the traffic to go quiet is not enough: the first reading
    after set_hover is usually the state BEFORE the repaint, and two equal
    readings then mean "nothing has happened yet" rather than "it is done" --
    which silently drops most of the moves. The chip's own overlay key is
    the arrival signal. (Keys are `<node id>#<piece>`.)
    """
    want = tile_id + "-play#"
    end = time.time() + timeout
    while time.time() < end:
        st = app.debug_state() or {}
        for key in (st.get("ov") or {}):
            if str(key).startswith(want):
                return _settled(app, timeout=1.5)
        time.sleep(0.05)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy", action="store_true",
                    help="reproduce the unnamed (path-keyed) row bitmap")
    ap.add_argument("--geometry", default="1280x720",
                    help="mpv window size; needs two tile rows to cross")
    ap.add_argument("--moves", type=int, default=12,
                    help="pointer moves between tiles to simulate")
    args = ap.parse_args()

    if args.legacy:
        _use_legacy_ids()

    from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
    from jellyfin_mpv_shim.mpvtk.layout import layout
    from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore, cache_dir
    from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
    from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore

    handle, ext = _spawn(args.geometry)
    app = MpvtkApp.attach(handle, ext=ext)
    strips = (StripStore(mem_store=MemoryStore()) if app.in_process
              else StripStore(cache_dir=cache_dir("mpvtk-probe-")))
    browser = MpvtkBrowser(app, _source(), strips=strips)
    thread = threading.Thread(target=lambda: app.run(browser.build),
                              daemon=True)
    thread.start()
    try:
        if not app.ready.wait(20):
            print("renderer never became ready")
            return 1
        browser.navigate({"kind": "grid", "server": "srv1",
                          "parent_id": "lib1", "collection_type": "movies",
                          "title": "Movies"})
        st = _settled(app, timeout=10)
        size = (st.get("w") or 1280, st.get("h") or 720)
        print("window %dx%d, backend %s, %d overlays on screen"
              % (size[0], size[1], h.BACKEND, st.get("overlays", 0)))
        if not st.get("overlays"):
            print("nothing rendered; is the window too small?")
            return 1

        # Tiles in scene order, so consecutive picks cross rows the way a
        # pointer sweeping down the grid does.
        nodes, _handlers = layout(browser.build(size), *size)
        tiles = [n["id"] for n in nodes
                 if n.get("hev") and str(n["id"]).startswith("grid-")]
        if len(tiles) < 2:
            print("no hoverable tiles in the scene")
            return 1

        # Only rows the window actually shows: the grid is virtualized and
        # the renderer clips to the scroll viewport, so a tile below the fold
        # is in the tree, draws nothing, and would be counted as a move that
        # cost nothing.
        rows = {}
        for n in nodes:
            # The chip is centred on the artwork, so a row counts as
            # crossable once its middle is inside the viewport -- a row
            # clipped at the bottom edge still draws, and still gets one.
            if n.get("id") in tiles and n["y"] + n["h"] / 2 < size[1] - 8:
                rows.setdefault(round(n["y"]), []).append(n["id"])
        ys = sorted(rows)
        if not ys:
            print("no fully visible tile rows in a %dx%d window" % size)
            return 1
        print("%d hoverable tiles across %d rows" % (len(tiles), len(ys)))

        base = _settled(app)
        adds0, bytes0 = base.get("ov_adds", 0), base.get("ov_bytes", 0)
        t0 = time.time()
        moves = 0
        for i in range(args.moves):
            # Alternate rows: crossing rows is the case the row bitmap's
            # identity decides, and it is what a pointer sweeping the grid
            # does most.
            row = ys[i % len(ys)]
            tile = rows[row][(i // len(ys)) % len(rows[row])]
            browser.tiles.set_hover(tile)
            if _wait_for_chip(app, tile) is None:
                print("   move %d: chip for %s never appeared" % (i, tile))
                continue
            moves += 1
        elapsed = time.time() - t0
        end = _settled(app)
        adds = end.get("ov_adds", 0) - adds0
        nbytes = end.get("ov_bytes", 0) - bytes0
        print("%s ids: %d pointer moves -> %d overlay-adds, %.1f MiB, %.1fs"
              % ("legacy" if args.legacy else "named", moves, adds,
                 nbytes / 1048576.0, elapsed))
        print("   per move: %.1f adds, %.2f MiB"
              % (adds / float(moves), nbytes / 1048576.0 / moves))
        return 0
    finally:
        try:
            app.quit()
            thread.join(timeout=5)
        finally:
            browser.shutdown()
            try:
                handle.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
