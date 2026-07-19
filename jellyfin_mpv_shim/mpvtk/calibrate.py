"""Font-metrics calibration harness.

Renders rows of text with a marker rect at the width layout.text_width
PREDICTS, screenshots the window, and measures where the text actually
ends. Prints per-row actual/predicted ratios — if layout and libass
agree, ratios are ~1.00.

Usage:  xvfb-run -a python3 -m jellyfin_mpv_shim.mpvtk.calibrate
"""

import os
import subprocess
import tempfile
import threading
import time

from .app import MpvtkApp
from .layout import text_width
from .widgets import Box, Column, Row, Spacer, Text

SAMPLES = [
    ("M" * 20, 40),
    ("i" * 40, 40),
    ("The quick brown fox jumps over 42 lazy dogs.", 32),
    ("Silent Harbor 1985 — Continue Watching", 22),
    # prefixes = caret positions mid-string: these are exactly where
    # the textbox draws the caret and selection edges
    ("The", 40),
    ("The q", 40),
    ("The quick", 40),
    ("The quick fox", 40),
    # kerning-heavy strings: per-char advances alone drift badly here
    ("TaTaTaTaTaTa", 40),
    ("AVAVAVAVAVAV", 40),
    ("ToWaYoToWaYo", 40),
]
X0 = 60
ROW_H = 58  # all SAMPLES rows must fit in the 720px window
MARKER = "ff2222"


def build(size):
    rows = []
    for text, fs in SAMPLES:
        w = text_width(text, fs)
        rows.append(
            Row(
                [
                    Text(text, size=fs, w=w + 4),
                    Box(w=3, h=fs + 10, bg=MARKER),
                    Spacer(),
                ],
                h=ROW_H,
                align="center",
            )
        )
    return Column(rows, pad=X0, w=size[0], h=size[1], gap=0)


def analyze(png):
    from PIL import Image

    img = Image.open(png).convert("RGB")
    # Root screenshots include the window's placement offset: find the
    # top of the mpv window (background #141414 vs root black).
    y_off = 0
    for y in range(img.height):
        r, g, b = img.getpixel((4, y))
        if abs(r - 0x14) < 5 and abs(g - 0x14) < 5 and abs(b - 0x14) < 5:
            y_off = y
            break
    print("window y-offset:", y_off)
    print("row  predicted  marker@  text-ends@  ratio")
    for i, (text, fs) in enumerate(SAMPLES):
        pred = text_width(text, fs)
        y0 = y_off + X0 + i * ROW_H + 14
        y1 = y0 + ROW_H - 28
        # scan the row band for the marker (red) and the rightmost
        # bright (text) pixel left of it
        marker_x = None
        for x in range(img.width - 1, 0, -1):
            for y in range(y0, y1, 3):
                r, g, b = img.getpixel((x, y))
                if r > 180 and g < 90 and b < 90:
                    marker_x = x
                    break
            if marker_x:
                break
        end_x = None
        limit = (marker_x or img.width) - 6
        for x in range(limit, 0, -1):
            for y in range(y0, y1, 2):
                r, g, b = img.getpixel((x, y))
                if r > 140 and g > 140 and b > 140:
                    end_x = x
                    break
            if end_x:
                break
        if marker_x is None or end_x is None:
            print("%3d  (not detected)" % i)
            continue
        actual = end_x - X0 + 1
        print(
            "%3d  %8.1f  %7d  %9d  %.3f   %r"
            % (i, pred, marker_x, end_x, actual / pred, text[:28])
        )


def main():
    app = MpvtkApp()
    out = os.path.join(tempfile.mkdtemp(prefix="mpvtk-cal-"), "cal.png")

    def drive():
        app.ready.wait(15)
        time.sleep(1.2)
        try:
            app.screenshot(out)
        except Exception:
            subprocess.run(
                ["import", "-window", "root", out], check=False,
                timeout=15,
            )
        time.sleep(0.2)
        app.quit()

    threading.Thread(target=drive, daemon=True).start()
    app.run(build)
    analyze(out)
    print("screenshot:", out)


if __name__ == "__main__":
    main()
