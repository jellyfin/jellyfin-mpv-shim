"""Reads a JMS_SCROLL_DIAG file (see renderer.lua, `state.scroll_diag_new`) and reports, per phase, what the
input delivered and what was drawn. Stdlib only; reads one file and prints; starts nothing.

The file holds E lines (one per wheel/touchpad event), F lines (one per rendered frame while a gesture is
live) and, when si_session.py wrote them, P lines that cut it into phases:  P <name> start|end <utc>.
Without P lines the whole file is one phase.

What it measures is what the RENDERER decided to draw and the input mpv delivered -- not what reached the
screen. A frame's drawn offset is the offset handed to the overlay, not a photograph of the display.
"""
import math
import statistics
import sys

SCALE_BINS = ((0.1, "<0.1"), (0.25, "0.1-0.25"), (0.5, "0.25-0.5"), (0.9, "0.5-0.9"), (0.99, "0.9-0.99"),
              (float("inf"), ">=0.99"))
E_COLS = ("t", "interval_ms", "dir", "axis", "scale", "cls", "c", "max", "before", "after", "want", "clamp",
          "glide0", "glide1", "snap")
F_COLS = ("t", "gap_ms", "c", "stored", "drawn", "d_drawn", "glide", "glide_pos", "snap")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse(text):
    """-> ({phase: {"E": [dict], "F": [dict]}}, malformed-line count), in file order. A file with no P lines is
    one phase called 'all'; with them, lines outside a start..end pair belong to no phase."""
    lines = text.splitlines()
    cut = any(l.startswith("P\t") and l.split("\t")[2:3] == ["start"] for l in lines)
    phases, cur, bad = {}, (None if cut else "all"), 0
    if not cut:
        phases["all"] = {"E": [], "F": []}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        kind = cols[0]
        if kind == "P" and len(cols) >= 3:
            if cols[2] == "start":
                cur = cols[1]
                phases.setdefault(cur, {"E": [], "F": []})
            elif cols[2] == "end":
                cur = None
            continue
        if kind in ("E", "F"):
            names = E_COLS if kind == "E" else F_COLS
            if len(cols) != len(names) + 1:
                bad += 1
            elif cur is not None:
                phases[cur][kind].append(dict(zip(names, cols[1:])))
    return phases, bad


def _pct(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round(p / 100.0 * (len(vals) - 1))))]


def input_stats(events):
    """What the device delivered."""
    scales = [_f(e["scale"]) for e in events]
    known = [s for s in scales if s is not None]
    ivals = [_f(e["interval_ms"]) for e in events if _f(e["interval_ms"]) is not None]
    t = [_f(e["t"]) for e in events]
    span = (max(t) - min(t)) if len(t) > 1 else 0.0
    hist = {label: 0 for _lim, label in SCALE_BINS}
    for s in known:
        for lim, label in SCALE_BINS:
            if s < lim:
                hist[label] += 1
                break
    cls = {"W": sum(1 for e in events if e["cls"] == "W"), "T": sum(1 for e in events if e["cls"] == "T")}
    w_fast = [e for e in events if e["cls"] == "W" and _f(e["interval_ms"]) is not None
              and _f(e["interval_ms"]) < 25]
    return {
        "events": len(events), "span_s": round(span, 2),
        "rate_per_s": round((len(events) - 1) / span, 1) if span > 0 else None,
        "interval_ms": {"p10": _pct(ivals, 10), "p50": _pct(ivals, 50), "p90": _pct(ivals, 90)},
        "scale": {"none_delivered": sum(1 for s in scales if s is None),
                  "min": min(known) if known else None, "p50": _pct(known, 50),
                  "max": max(known) if known else None},
        "scale_hist": hist, "class": cls,
        "scale_ge_099": sum(1 for s in known if s >= 0.99),
        "scale_exactly_1": sum(1 for s in known if s == 1.0),
        "w_within_25ms_of_previous": len(w_fast),
    }


def offset_stats(events):
    """What the events asked for and how the container answered."""
    moved = [e for e in events if _f(e["before"]) is not None and _f(e["after"]) is not None]
    dead = sum(1 for e in moved if _f(e["after"]) == _f(e["before"]))
    return {"clamp_lo": sum(1 for e in events if e["clamp"] == "lo"),
            "clamp_hi": sum(1 for e in events if e["clamp"] == "hi"),
            "no_target": sum(1 for e in events if e["c"] == "-"),
            "events_that_moved_nothing": dead,
            "glide_started": sum(1 for e in events if e["glide0"] == "0" and e["glide1"] == "1"),
            "snap_flags": sorted({e["snap"] for e in events})}


def frame_stats(frames, events):
    """Per-frame drawn motion during the gesture (first event .. last event + the glide's tail)."""
    if not frames:
        return {"frames": 0}
    t0 = min(_f(e["t"]) for e in events) if events else _f(frames[0]["t"])
    t1 = max(_f(e["t"]) for e in events) if events else _f(frames[-1]["t"])
    inside = [f for f in frames if t0 - 1e-9 <= _f(f["t"]) <= t1 + 1e-9]
    d = [_f(f["d_drawn"]) for f in inside if _f(f["d_drawn"]) is not None]
    gaps = [_f(f["gap_ms"]) for f in frames if _f(f["gap_ms"]) is not None]
    lag = [abs(_f(f["stored"]) - _f(f["drawn"])) for f in frames
           if _f(f["stored"]) is not None and _f(f["drawn"]) is not None]
    out = {"frames": len(frames), "frames_in_gesture": len(inside),
           "gap_ms": {"p50": _pct(gaps, 50), "p90": _pct(gaps, 90), "max": max(gaps) if gaps else None},
           "glide_frames": sum(1 for f in frames if f["glide"] == "1"),
           "lag_px": {"mean": round(statistics.mean(lag), 1) if lag else None, "max": round(max(lag), 1) if lag else None}}
    if len(d) >= 3:
        mean = statistics.mean(abs(x) for x in d)
        # speed variation: stdev of the signed per-frame displacement over its mean (the direction of a
        # phase can reverse, so use magnitudes)
        mags = [abs(x) for x in d]
        sd = statistics.pstdev(mags)
        out.update({
            "mean_abs_px_per_frame": round(mean, 1),
            "max_abs_px_per_frame": max(mags),
            "variation": round(sd / mean, 2) if mean else None,
            "still_frames": sum(1 for x in mags if x == 0),
            "spikes_over_2x_median": sum(1 for x in mags if x > 2 * (statistics.median(mags) or 1e9)),
        })
    return out


def analyze(text):
    phases, bad = parse(text)
    rep = {"bad_lines": bad, "phases": {}}
    for name, data in phases.items():
        rep["phases"][name] = {"input": input_stats(data["E"]), "offsets": offset_stats(data["E"]),
                               "frames": frame_stats(data["F"], data["E"])}
    return rep


def render(rep):
    out = []
    for name, p in rep["phases"].items():
        i, o, f = p["input"], p["offsets"], p["frames"]
        out.append("== %s" % name)
        out.append("  input   %d events over %.1fs (%s/s)  interval ms p10/p50/p90 %s/%s/%s" % (
            i["events"], i["span_s"], i["rate_per_s"], i["interval_ms"]["p10"], i["interval_ms"]["p50"],
            i["interval_ms"]["p90"]))
        s = i["scale"]
        out.append("  scale   min %s  median %s  max %s  none-delivered %d | class W %d  T %d | >=0.99: %d (exactly 1.0: %d, "
                   "of those %d within 25 ms of the previous event)" % (
                       s["min"], s["p50"], s["max"], s["none_delivered"], i["class"]["W"], i["class"]["T"],
                       i["scale_ge_099"], i["scale_exactly_1"], i["w_within_25ms_of_previous"]))
        out.append("  scales  " + "  ".join("%s:%d" % kv for kv in i["scale_hist"].items()))
        out.append("  offsets glides started %d  clamps lo/hi %d/%d  moved-nothing %d  no-target %d  snap %s" % (
            o["glide_started"], o["clamp_lo"], o["clamp_hi"], o["events_that_moved_nothing"], o["no_target"],
            ",".join(o["snap_flags"]) or "-"))
        if f.get("frames"):
            line = "  frames  %d (%d in gesture)  gap p50/p90/max %s/%s/%s ms  glide frames %d  stored-drawn lag mean/max %s/%s px" % (
                f["frames"], f["frames_in_gesture"], f["gap_ms"]["p50"], f["gap_ms"]["p90"], f["gap_ms"]["max"],
                f["glide_frames"], f["lag_px"]["mean"], f["lag_px"]["max"])
            out.append(line)
            if "variation" in f:
                out.append("  motion  px/frame mean %s max %s  speed variation %s  still frames %d  spikes>2x median %d" % (
                    f["mean_abs_px_per_frame"], f["max_abs_px_per_frame"], f["variation"], f["still_frames"],
                    f["spikes_over_2x_median"]))
    if rep["bad_lines"]:
        out.append("(%d malformed lines skipped)" % rep["bad_lines"])
    return "\n".join(out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: si_analyze.py SCROLL_DIAG_FILE", file=sys.stderr)
        return 64
    with open(argv[0], encoding="utf-8", errors="replace") as fh:
        print(render(analyze(fh.read())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
