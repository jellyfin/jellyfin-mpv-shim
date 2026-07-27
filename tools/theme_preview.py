"""Screenshot a themed widget sample through real mpv.

Usage: SHOT_THEME=jf-light xvfb-run -a python3 -m tools.theme_preview

The scene tests read colours off scene nodes, which proves the tokens reach
the tree. It cannot see the controls the RENDERER draws for itself -- the
dropdown, its popup, the text field, the scrollbar, the tooltip -- because
Python never sends a colour for those. Those are exactly the ones that were
unthemeable, so they are exactly the ones worth photographing.
"""
import os
import subprocess
import tempfile
import threading
import time

from jellyfin_mpv_shim.mpvtk.app import MpvtkApp                  # noqa: E402
from jellyfin_mpv_shim.mpvtk.widgets import (                     # noqa: E402
    Box, Button, Checkbox, Column, Dropdown, Gradient, Progress, Row, Slider,
    Spacer, Stack, Table, Text, TextBox, VScroll)
from jellyfin_mpv_shim.mpvtk_browser import theme                 # noqa: E402

THEME = os.environ.get("SHOT_THEME", "default")


def build(size):
    w, h = size
    rows = [{"cells": ["1", "Alpha", "2001"], "id": "r0"},
            {"cells": ["2", "Beta", "2003"], "id": "r1", "selected": True},
            {"cells": ["3", "Gamma", "2011"], "id": "r2"}]
    # The top bar, with the theme's own gradient if it has one -- this is
    # what window_chrome.chrome_bar builds in the real app.
    top = Row([Text("  %s" % theme.active()["name"], size=22, bold=True)],
              h=60, w=w, align="center",
              bg=None if theme.topbar_gradient() else theme.PANEL_BG)
    bar_stops = theme.topbar_gradient()
    if bar_stops:
        top = Stack([Gradient(stops=bar_stops, axis="x", w=w, h=60), top],
                    w=w, h=60)
    body = Column([
        Text("every control below is renderer-drawn or widget-default",
             size=20),
        Row([Button("Primary", id="b1"),
             Button("With icon", id="b2", icon="play_arrow"),
             Button("Flat", id="b3", icon="favorite", flat=True),
             Spacer()], gap=10, align="center"),
        Row([TextBox("tb", text="a text field", w=240),
             TextBox("tb2", placeholder="placeholder", w=240),
             Spacer()], gap=10, align="center"),
        Row([Dropdown("dd", ["Dropdown closed", "Second", "Third"],
                      selected=0, w=240),
             Checkbox("Checked", True), Checkbox("Unchecked", False),
             Spacer()], gap=16, align="center"),
        Row([Slider("sl", 0.45, w=240), Progress(0.6, w=240), Spacer()],
            gap=16, align="center"),
        Table([{"label": "#", "w": 40}, {"label": "Title", "flex": 1},
               {"label": "Year", "w": 80, "align": "right"}], rows),
        Box(h=400),   # give the scroll container something to scroll
    ], pad=20, gap=18, align="stretch")
    page = Column([top, VScroll(body, id="page", flex=1, scrollbar=True)],
                  w=w, h=h,
                  bg=None if theme.window_gradient() else theme.WINDOW_BG)
    stops = theme.window_gradient()
    if not stops:
        return page
    return Stack([Gradient(stops=stops, axis="y", w=w, h=h), page], w=w, h=h)


def main():
    cfg = theme.apply(THEME)
    theme.apply_to_toolkit(glow=cfg.get("glow", False))
    app = MpvtkApp()
    out = os.path.join(tempfile.mkdtemp(prefix="mpvtk-theme-"),
                       "%s.png" % THEME)

    def drive():
        app.ready.wait(15)
        time.sleep(1.4)
        try:
            app.screenshot(out)
        except Exception:
            subprocess.run(["import", "-window", "root", out], check=False,
                           timeout=15)
        time.sleep(0.3)
        app.quit()

    threading.Thread(target=drive, daemon=True).start()
    app.run(build)
    print(out)


if __name__ == "__main__":
    main()
