"""Gradient spike: render multi-stop colour ramps through real mpv/libass and
measure them against an ideal linear interpolation.

The question is whether layered blurred-edge ramps are good enough to stand in
for CSS linear-gradient(), which several jellyfin-web themes lean on. Banding
is the obvious alternative and this codebase already rejected it once (the old
lua OSC), so the bar is: does the layered version land close to a true lerp,
and what does it cost?

Usage: xvfb-run -a python3 -m tools.gradient_fidelity
"""
import os
import subprocess
import sys
import tempfile
import threading
import time

from jellyfin_mpv_shim.mpvtk.app import MpvtkApp            # noqa: E402
from jellyfin_mpv_shim.mpvtk.widgets import (Column, Gradient,  # noqa: E402
                                             Stack, Text)

BAND_H = 150

# Real gradients from jellyfin-web themes, plus a worst case.
CASES = [
    ("wmc bg (3-stop, vertical)", "y",
     [(0.0, "0f3562"), (0.5, "1162a4"), (1.0, "03215f")]),
    ("purplehaze header (5-stop, horizontal)", "x",
     [(0.0, "000420"), (0.18, "06256f"), (0.38, "2b052b"),
      (0.81, "06256f"), (1.0, "000420")]),
    ("blueradiance ribbon (5-stop, horizontal)", "x",
     [(0.0, "291a31"), (0.25, "033664"), (0.5, "011432"),
      (0.75, "141a3a"), (1.0, "291a31")]),
    ("worst case: black -> white (2-stop, vertical)", "y",
     [(0.0, "000000"), (1.0, "ffffff")]),
]


def build(size):
    w, h = size
    rows = []
    for label, axis, stops in CASES:
        rows.append(Stack([
            Gradient(stops=stops, axis=axis, w=w, h=BAND_H),
            Text(label, size=15, color="ffffff", anchor="nw", dx=8, dy=6),
        ], w=w, h=BAND_H))
    return Column(rows, w=w, h=h)


def lerp_srgb(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def ideal(stops, t):
    """What CSS linear-gradient() would produce at position t."""
    cols = [(int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
            for _p, c in stops]
    pos = [p for p, _c in stops]
    if t <= pos[0]:
        return cols[0]
    for i in range(1, len(pos)):
        if t <= pos[i]:
            span = pos[i] - pos[i - 1]
            f = 0.0 if span == 0 else (t - pos[i - 1]) / span
            return lerp_srgb(cols[i - 1], cols[i], f)
    return cols[-1]


def analyze(png):
    from PIL import Image

    img = Image.open(png).convert("RGB")
    print("screenshot %dx%d" % img.size)
    # The window may be offset inside a root screenshot; find the first row
    # that matches the first case's top colour.
    want = ideal(CASES[0][2], 0.0)
    y_off = 0
    for y in range(img.height):
        px = img.getpixel((img.width // 2, y))
        if all(abs(a - b) < 12 for a, b in zip(px, want)):
            y_off = y
            break
    print("window y-offset: %d\n" % y_off)

    ok = True
    for i, (label, axis, stops) in enumerate(CASES):
        y0 = y_off + i * BAND_H
        errs = []
        # Sample away from the band edges, where neighbouring bands' blur
        # bleeds across -- that bleed is itself a finding, reported below.
        for step in range(6, 95):
            t = step / 100.0
            if axis == "y":
                x = img.width // 2
                y = y0 + int(t * BAND_H)
            else:
                x = int(t * img.width)
                y = y0 + BAND_H // 2
            if y >= img.height or x >= img.width:
                continue
            got = img.getpixel((x, y))
            exp = ideal(stops, t)
            errs.append((max(abs(a - b) for a, b in zip(got, exp)), t,
                         got, exp))
        if not errs:
            print("  %-42s no samples" % label)
            continue
        worst = max(errs)
        mean = sum(e[0] for e in errs) / len(errs)
        flag = "  " if worst[0] <= 24 else "!!"
        if worst[0] > 24:
            ok = False
        print("%s %-42s mean dE %5.1f   worst dE %3d at t=%.2f  "
              "(got %s want %s)"
              % (flag, label, mean, worst[0], worst[1], worst[2], worst[3]))
    print("\nverdict:", "usable" if ok else "NOT usable as-is")
    return ok


def main():
    app = MpvtkApp()
    out = os.path.join(tempfile.mkdtemp(prefix="mpvtk-grad-"), "grad.png")

    def drive():
        app.ready.wait(15)
        time.sleep(1.5)
        try:
            app.screenshot(out)
        except Exception:
            subprocess.run(["import", "-window", "root", out], check=False,
                           timeout=15)
        time.sleep(0.3)
        app.quit()

    threading.Thread(target=drive, daemon=True).start()
    app.run(build)
    analyze(out)
    print("screenshot:", out)


if __name__ == "__main__":
    main()
