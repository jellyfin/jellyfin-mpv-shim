"""Render multi-stop colour gradients through real mpv/libass and measure them.

Gradients are how several of jellyfin-web's themes carry their identity, and
mpvtk draws them by layering blurred-edge ramps (see widgets.Gradient). libass
has no gradient primitive; the alternative is banding, which this codebase
already rejected once when the old lua OSC's banded gradient came out visibly
stepped. So the question is how close the layered version gets.

TWO measurements, and the second is the one that matters.

``colour error`` is how far each sampled position lands from what CSS
linear-gradient() would produce. Useful, and on its own actively misleading:
it is blind to the artifact you actually see. Every gaussian layer begins and
ends at ~zero slope, so each extra layer puts a flat spot in the ramp, and
flat spots read as bands. Optimising this metric alone leads straight to
subdividing every segment -- which scores better and looks worse.

``slope profile`` is the correction. A gradient with N interior stops has N
places where the climb rate legitimately changes, and CSS has those too.
More flat runs than that means we introduced them.

Usage: xvfb-run -a python3 -m tools.gradient_fidelity
"""
import os
import subprocess
import tempfile
import threading
import time

from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
from jellyfin_mpv_shim.mpvtk.widgets import Column, Gradient, Stack, Text

# Real gradients from jellyfin-web themes, plus controls.
CASES = [
    ("wmc bg (3-stop, vertical)", "y",
     [(0.0, "0f3562"), (0.5, "1162a4"), (1.0, "03215f")]),
    ("purplehaze header (5-stop, horizontal)", "x",
     [(0.0, "000420"), (0.18, "06256f"), (0.38, "2b052b"),
      (0.81, "06256f"), (1.0, "000420")]),
    ("blueradiance ribbon (5-stop, horizontal)", "x",
     [(0.0, "291a31"), (0.25, "033664"), (0.5, "011432"),
      (0.75, "141a3a"), (1.0, "291a31")]),
    ("black -> white (2-stop, vertical)", "y",
     [(0.0, "000000"), (1.0, "ffffff")]),
    # The same ramp with redundant stops. CSS renders these identically;
    # here each extra stop is another junction, which is why "add more stops
    # for smoothness" is exactly backwards.
    ("...same ramp, 5 redundant stops", "y",
     [(0.0, "000000"), (0.25, "404040"), (0.5, "808080"),
      (0.75, "bfbfbf"), (1.0, "ffffff")]),
]

BAND_H = 130          # 5 bands inside a 720-tall window, with room to spare


def _lerp(a, b, t):
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
            return _lerp(cols[i - 1], cols[i],
                         0.0 if span == 0 else (t - pos[i - 1]) / span)
    return cols[-1]


def build(size):
    w, h = size
    return Column([
        Stack([Gradient(stops=stops, axis=axis, w=w, h=BAND_H),
               Text(label, size=14, color="ff3333", anchor="nw", dx=8, dy=5)],
              w=w, h=BAND_H)
        for label, axis, stops in CASES
    ], w=w, h=h)


def _profile(img, y0, axis, width):
    """Luma along the band, averaged ACROSS the gradient and then smoothed.

    Both steps matter. A single scanline of a shallow ramp climbs ~2 levels
    per pixel, so 8-bit quantisation and the screenshot's dither are the same
    size as the signal; differencing that gives noise, not slope. Averaging
    perpendicular to the gradient (where it is constant by construction) and
    box-filtering along it recovers the real curve.
    """
    if axis == "y":
        xs = range(width // 4, 3 * width // 4, 7)
        raw = [sum(sum(img.getpixel((x, y0 + y))) / 3.0 for x in xs) / len(xs)
               for y in range(3, BAND_H - 3)]
    else:
        ys = range(y0 + 20, y0 + BAND_H - 8, 5)
        raw = [sum(sum(img.getpixel((x, y))) / 3.0 for y in ys) / len(ys)
               for x in range(3, width - 3)]
    win = max(2, len(raw) // 24)
    sm = [sum(raw[max(0, i - win):i + win + 1])
          / len(raw[max(0, i - win):i + win + 1]) for i in range(len(raw))]
    step = max(1, len(sm) // 60)
    return sm[::step]


def analyze(png):
    from PIL import Image

    img = Image.open(png).convert("RGB")
    print("screenshot %dx%d" % img.size)
    want = ideal(CASES[0][2], 0.0)
    y_off = 0
    for y in range(img.height):
        px = img.getpixel((img.width // 2, y))
        if all(abs(a - b) < 12 for a, b in zip(px, want)):
            y_off = y
            break
    print("window y-offset: %d\n" % y_off)

    even = True
    print("colour error vs an ideal lerp")
    for i, (label, axis, stops) in enumerate(CASES):
        y0 = y_off + i * BAND_H
        errs = []
        for step in range(6, 95):
            t = step / 100.0
            x, y = ((img.width // 2, y0 + int(t * BAND_H)) if axis == "y"
                    else (int(t * img.width), y0 + BAND_H // 2))
            if y >= img.height or x >= img.width:
                continue
            got, exp = img.getpixel((x, y)), ideal(stops, t)
            errs.append((max(abs(a - b) for a, b in zip(got, exp)), t))
        if not errs:
            print("   %-42s no samples" % label)
            continue
        worst, mean = max(errs), sum(e[0] for e in errs) / len(errs)
        print("   %-42s mean %5.1f   worst %3d at t=%.2f"
              % (label, mean, worst[0], worst[1]))
    print("   (informational. A single gaussian per segment EASES rather than\n"
          "    running straight, so a full-range 2-stop ramp sits ~30 off an\n"
          "    ideal lerp by design. Flattening that curve means more layers,\n"
          "    and more layers is what the slope profile below rules out.)")

    print("\nslope profile  (# climbing, . slow, _ near-flat)")
    for i, (label, axis, stops) in enumerate(CASES):
        prof = _profile(img, y_off + i * BAND_H, axis, img.width)
        slope = [prof[j + 1] - prof[j] for j in range(len(prof) - 1)]
        core = slope[2:-2] or slope
        # Relative to the fastest part of the ramp, not the mean: what we are
        # hunting is "the climb stalled here", and a gradient with several
        # segments has a different mean in each.
        peak = max(abs(s) for s in core) or 1.0
        mean = sum(abs(s) for s in core) / len(core) or 1.0
        # INTERIOR flat runs only. A single gaussian is slow at both ends by
        # construction -- that is the ease we chose, not a junction -- so a
        # run touching either end of the band is not evidence of anything.
        runs, run_start = [], None
        for j, s in enumerate(core):
            if abs(s) < peak * 0.25:
                if run_start is None:
                    run_start = j
            elif run_start is not None:
                runs.append((run_start, j - 1))
                run_start = None
        if run_start is not None:
            runs.append((run_start, len(core) - 1))
        flats = sum(1 for a, b in runs if a > 0 and b < len(core) - 1)
        budget = len(stops) - 2
        good = flats <= budget
        even = even and good
        bar = "".join("#" if abs(s) > mean else
                      ("." if abs(s) > peak * 0.25 else "_") for s in core)
        print("%s %-42s %s" % ("  " if good else "!!", label, bar[:50]))
        print("   %-42s %d interior flat run(s), %d allowed by its stops"
              % ("", flats, budget))

    print("\nverdict:", "usable" if even else "NOT usable as-is")
    return even


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
